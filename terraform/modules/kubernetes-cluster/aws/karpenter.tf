// Karpenter's AWS half: the two identities, the interruption queue and the discovery tag. The
// NodePools and EC2NodeClasses are Kubernetes objects in helm/charts/node-pools.
//
// The managed group stays: Karpenter's chart keeps the controller off Karpenter nodes, so a
// node it did not make has to exist for the controller to run.
//
// The controller policy is AWS's published one for the pinned release, minus the
// instance-profile lifecycle statements (this file creates the profile) and plus karpenter_kms,
// which the published one lacks. The two instance-profile reads stay because the controller
// makes them anyway (docs/deployment/aws-configuration.md).

locals {
  // The tag both selector terms match on, valued with the cluster name so clusters sharing a VPC keep their subnets apart.
  karpenter_discovery_key = "karpenter.sh/discovery"

  // One queue, four events, told apart by detail-type. AWS Health carries scheduled retirement
  // and degradation, the other three are EC2's.
  karpenter_events = {
    spot-interruption     = { source = "aws.ec2", detail_type = "EC2 Spot Instance Interruption Warning" }
    rebalance             = { source = "aws.ec2", detail_type = "EC2 Instance Rebalance Recommendation" }
    instance-state-change = { source = "aws.ec2", detail_type = "EC2 Instance State-change Notification" }
    health                = { source = "aws.health", detail_type = "AWS Health Event" }
  }

  // Karpenter's published queue retention: a notice older than five minutes names an instance already gone.
  karpenter_interruption_retention_seconds = 300
}

// ---------------------------------------------------------------------------
// Discovery -- what the EC2NodeClass selector terms match
// ---------------------------------------------------------------------------

// EKS creates the cluster security group, so aws_ec2_tag tags it. The subnets carry the tag in
// vpc.tf instead, because aws_ec2_tag against one fights aws_subnet's own tag management.
resource "aws_ec2_tag" "karpenter_cluster_security_group" {
  resource_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  key         = local.karpenter_discovery_key
  value       = var.name
}

// ---------------------------------------------------------------------------
// Interruption queue -- two minutes' notice, and what Karpenter does with it
// ---------------------------------------------------------------------------

resource "aws_sqs_queue" "karpenter" {
  name = var.name

  // A notice is worthless once the instance is gone, so an old message is deleted, not replayed at a restarted controller.
  message_retention_seconds = local.karpenter_interruption_retention_seconds

  // SQS-managed encryption: the messages are instance ids and event types, and a CMK would need
  // an EventBridge grant in the key policy for nothing.
  sqs_managed_sse_enabled = true
}

data "aws_iam_policy_document" "karpenter_queue" {
  statement {
    sid     = "AllowEventBridgeToEnqueue"
    effect  = "Allow"
    actions = ["sqs:SendMessage"]

    resources = [aws_sqs_queue.karpenter.arn]

    principals {
      type = "Service"
      // The service principals EventBridge delivers to a queue as. Fixed by AWS.
      identifiers = ["events.amazonaws.com", "sqs.amazonaws.com"]
    }

    // Without this, any account's EventBridge rule that learns the queue ARN can enqueue to it,
    // and a forged spot-interruption naming a real instance id drains a node. AWS's published
    // CloudFormation omits the condition, so this tightens upstream.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.provision.account]
    }
  }

  statement {
    sid     = "DenyHTTP"
    effect  = "Deny"
    actions = ["sqs:*"]

    resources = [aws_sqs_queue.karpenter.arn]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "karpenter" {
  queue_url = aws_sqs_queue.karpenter.id
  policy    = data.aws_iam_policy_document.karpenter_queue.json
}

// Without these rules nothing ever writes to the queue.
resource "aws_cloudwatch_event_rule" "karpenter" {
  for_each = local.karpenter_events

  name = "${var.name}-karpenter-${each.key}"

  event_pattern = jsonencode({
    source        = [each.value.source]
    "detail-type" = [each.value.detail_type]
  })
}

resource "aws_cloudwatch_event_target" "karpenter" {
  for_each = local.karpenter_events

  rule = aws_cloudwatch_event_rule.karpenter[each.key].name
  arn  = aws_sqs_queue.karpenter.arn

  // Upstream's spelling, which Karpenter matches in its logs.
  target_id = "KarpenterInterruptionQueueTarget"
}

// ---------------------------------------------------------------------------
// Node identity -- its own role so the controller's PassRole reaches exactly one role, with the
// same policy set as eks.tf's managed-group role.
// ---------------------------------------------------------------------------

resource "aws_iam_role" "karpenter_node" {
  name                 = "${var.name}-karpenter-node"
  path                 = var.iam_path
  assume_role_policy   = data.aws_iam_policy_document.ec2_assume.json
  permissions_boundary = var.permissions_boundary
}

resource "aws_iam_role_policy_attachment" "karpenter_node" {
  for_each = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEKS_CNI_Policy",
    // PullOnly, not ReadOnly: a node never describes a repository.
    "AmazonEC2ContainerRegistryPullOnly",
    // Session Manager is how an operator reaches a node in a private subnet.
    "AmazonSSMManagedInstanceCore",
  ])

  role       = aws_iam_role.karpenter_node.name
  policy_arn = "${local.managed_policy_prefix}/${each.value}"
}

// A profile rather than a role, so the controller never needs iam:CreateInstanceProfile and a
// private cluster with no route to the IAM endpoint can still launch nodes.
resource "aws_iam_instance_profile" "karpenter_node" {
  name = "${var.name}-karpenter-node"
  path = var.iam_path
  role = aws_iam_role.karpenter_node.name
}

// With authentication_mode API the aws-auth ConfigMap is read by nobody, so this entry is what
// lets a Karpenter node register. EKS adds one for a managed group itself.
resource "aws_eks_access_entry" "karpenter_node" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_iam_role.karpenter_node.arn

  // EC2_LINUX grants system:nodes and the node username itself, so neither kubernetes_groups nor an access policy may be set.
  type = "EC2_LINUX"
}

// ---------------------------------------------------------------------------
// Controller identity
// ---------------------------------------------------------------------------

resource "aws_iam_role" "karpenter" {
  name                 = "${var.name}-karpenter"
  path                 = var.iam_path
  assume_role_policy   = data.aws_iam_policy_document.pod_identity_trust.json
  permissions_boundary = var.permissions_boundary
}

data "aws_iam_policy_document" "karpenter_lifecycle" {
  statement {
    sid     = "AllowScopedEC2InstanceAccessActions"
    effect  = "Allow"
    actions = ["ec2:RunInstances", "ec2:CreateFleet"]

    resources = [
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}::image/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}::snapshot/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:security-group/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:subnet/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:capacity-reservation/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:placement-group/*",
    ]
  }

  statement {
    sid     = "AllowScopedEC2LaunchTemplateAccessActions"
    effect  = "Allow"
    actions = ["ec2:RunInstances", "ec2:CreateFleet"]

    resources = ["arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:launch-template/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/kubernetes.io/cluster/${var.name}"
      values   = ["owned"]
    }

    condition {
      test     = "StringLike"
      variable = "aws:ResourceTag/karpenter.sh/nodepool"
      values   = ["*"]
    }
  }

  statement {
    sid     = "AllowScopedEC2InstanceActionsWithTags"
    effect  = "Allow"
    actions = ["ec2:RunInstances", "ec2:CreateFleet", "ec2:CreateLaunchTemplate"]

    resources = [
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:fleet/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:instance/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:volume/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:network-interface/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:launch-template/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:spot-instances-request/*",
    ]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes.io/cluster/${var.name}"
      values   = ["owned"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/eks:eks-cluster-name"
      values   = [var.name]
    }

    condition {
      test     = "StringLike"
      variable = "aws:RequestTag/karpenter.sh/nodepool"
      values   = ["*"]
    }
  }

  statement {
    sid     = "AllowScopedResourceCreationTagging"
    effect  = "Allow"
    actions = ["ec2:CreateTags"]

    resources = [
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:fleet/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:instance/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:volume/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:network-interface/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:launch-template/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:spot-instances-request/*",
    ]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes.io/cluster/${var.name}"
      values   = ["owned"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/eks:eks-cluster-name"
      values   = [var.name]
    }

    // Tagging only as part of the creating call, so a stolen credential cannot relabel an existing instance into the cluster.
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances", "CreateFleet", "CreateLaunchTemplate"]
    }

    condition {
      test     = "StringLike"
      variable = "aws:RequestTag/karpenter.sh/nodepool"
      values   = ["*"]
    }
  }

  statement {
    sid     = "AllowScopedResourceTagging"
    effect  = "Allow"
    actions = ["ec2:CreateTags"]

    resources = ["arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/kubernetes.io/cluster/${var.name}"
      values   = ["owned"]
    }

    condition {
      test     = "StringLike"
      variable = "aws:ResourceTag/karpenter.sh/nodepool"
      values   = ["*"]
    }

    condition {
      test     = "StringEqualsIfExists"
      variable = "aws:RequestTag/eks:eks-cluster-name"
      values   = [var.name]
    }

    // The three keys Karpenter rewrites after launch, and no others.
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = ["eks:eks-cluster-name", "karpenter.sh/nodeclaim", "Name"]
    }
  }

  statement {
    sid     = "AllowScopedDeletion"
    effect  = "Allow"
    actions = ["ec2:TerminateInstances", "ec2:DeleteLaunchTemplate"]

    resources = [
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:instance/*",
      "arn:${data.aws_partition.current.partition}:ec2:${var.provision.region}:*:launch-template/*",
    ]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/kubernetes.io/cluster/${var.name}"
      values   = ["owned"]
    }

    condition {
      test     = "StringLike"
      variable = "aws:ResourceTag/karpenter.sh/nodepool"
      values   = ["*"]
    }
  }
}

resource "aws_iam_role_policy" "karpenter_lifecycle" {
  name   = "${var.name}-karpenter-lifecycle"
  role   = aws_iam_role.karpenter.name
  policy = data.aws_iam_policy_document.karpenter_lifecycle.json
}

// RunInstances and CreateFleet run under this role, so EC2 checks this role, not the node role,
// for KMS permission before attaching an encrypted root volume. Without it every launch fails
// Client.InvalidKMSKey.InvalidState.
//
// jsonencode() rather than a data "aws_iam_policy_document", for the reason kms.tf gives.
locals {
  karpenter_kms_policy_statements = [
    {
      // The action list AWS documents for launching EC2 instances with a customer managed key
      // (autoscaling/ec2/userguide/key-policy-requirements-EBS-encryption.html).
      Sid    = "AllowEBSEncryptionActions"
      Effect = "Allow"
      Action = [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:ReEncrypt*",
        "kms:GenerateDataKey*",
        "kms:DescribeKey",
      ]
      Resource = local.kms_key_arn
      // Usable only through EC2: the controller has no reason to touch this key outside a node launch.
      Condition = {
        StringEquals = {
          "kms:ViaService" = "ec2.${var.provision.region}.amazonaws.com"
        }
      }
    },
    {
      // CreateGrant lets the controller delegate part of its key permissions to EC2 for the instance's life.
      Sid      = "AllowEBSEncryptionGrants"
      Effect   = "Allow"
      Action   = "kms:CreateGrant"
      Resource = local.kms_key_arn
      Condition = {
        StringEquals = {
          "kms:ViaService" = "ec2.${var.provision.region}.amazonaws.com"
        }
        // Only grants an AWS service creates for itself, as AWS's key-policy documentation requires on CreateGrant.
        Bool = {
          "kms:GrantIsForAWSResource" = "true"
        }
      }
    },
  ]
}

resource "aws_iam_role_policy" "karpenter_kms" {
  name = "${var.name}-karpenter-kms"
  role = aws_iam_role.karpenter.name

  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.karpenter_kms_policy_statements
  })
}

data "aws_iam_policy_document" "karpenter_iam" {
  statement {
    sid     = "AllowPassingInstanceRole"
    effect  = "Allow"
    actions = ["iam:PassRole"]

    // One role, the instance profile's: the controller cannot hand a node any other identity.
    resources = [aws_iam_role.karpenter_node.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      // Both EC2 principals AWS documents: the China partitions use the second.
      values = ["ec2.amazonaws.com", "ec2.amazonaws.com.cn"]
    }
  }

  // The instance-profile garbage collector and every EC2NodeClass finalizer
  // list profiles, and a denied list stops the finalizer before it deletes the
  // class's launch templates.
  statement {
    sid       = "AllowUnscopedInstanceProfileListAction"
    effect    = "Allow"
    actions   = ["iam:ListInstanceProfiles"]
    resources = ["*"]
  }

  // The finalizer also reads a legacy profile name Karpenter derives from the
  // cluster, so the read reaches every profile in the account.
  statement {
    sid       = "AllowInstanceProfileReadActions"
    effect    = "Allow"
    actions   = ["iam:GetInstanceProfile"]
    resources = ["arn:${data.aws_partition.current.partition}:iam::${var.provision.account}:instance-profile/*"]
  }
}

resource "aws_iam_role_policy" "karpenter_iam" {
  name   = "${var.name}-karpenter-iam"
  role   = aws_iam_role.karpenter.name
  policy = data.aws_iam_policy_document.karpenter_iam.json
}

data "aws_iam_policy_document" "karpenter_discovery" {
  statement {
    sid     = "AllowAPIServerEndpointDiscovery"
    effect  = "Allow"
    actions = ["eks:DescribeCluster"]

    // settings.clusterEndpoint is left unset in the chart, so the controller
    // asks EKS for it at startup.
    resources = [aws_eks_cluster.this.arn]
  }

  statement {
    sid    = "AllowInterruptionQueueActions"
    effect = "Allow"

    actions = [
      "sqs:DeleteMessage",
      "sqs:GetQueueUrl",
      "sqs:ReceiveMessage",
    ]

    resources = [aws_sqs_queue.karpenter.arn]
  }

  statement {
    sid    = "AllowRegionalReadActions"
    effect = "Allow"

    actions = [
      "ec2:DescribeCapacityReservations",
      "ec2:DescribeImages",
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
      "ec2:DescribeInstanceTypeOfferings",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeLaunchTemplates",
      "ec2:DescribePlacementGroups",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeSpotPriceHistory",
      "ec2:DescribeSubnets",
    ]

    // The Describe calls take no resource ARN, so the region condition is the
    // only fence available on them.
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.provision.region]
    }
  }

  statement {
    sid     = "AllowSSMReadActions"
    effect  = "Allow"
    actions = ["ssm:GetParameter"]

    // Where AWS publishes the EKS-optimised AMI ids the al2023 alias resolves through. AWS owns them, so the ARN has no account.
    resources = ["arn:${data.aws_partition.current.partition}:ssm:${var.provision.region}::parameter/aws/service/*"]
  }

  statement {
    sid     = "AllowPricingReadActions"
    effect  = "Allow"
    actions = ["pricing:GetProducts"]

    // Pricing is a global service with no resource-level permissions; without
    // it Karpenter cannot rank candidate types by cost.
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "karpenter_discovery" {
  name   = "${var.name}-karpenter-discovery"
  role   = aws_iam_role.karpenter.name
  policy = data.aws_iam_policy_document.karpenter_discovery.json
}

resource "aws_eks_pod_identity_association" "karpenter" {
  cluster_name = aws_eks_cluster.this.name

  // Namespace and service account the chart installs under (argocd/appsets/layer1-addons.yaml),
  // which names it because the chart's default is generated from the release.
  namespace       = "kube-system"
  service_account = "karpenter"

  role_arn = aws_iam_role.karpenter.arn
}
