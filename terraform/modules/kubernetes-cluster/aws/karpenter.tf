// Karpenter -- the node provisioner for everything the managed group does not
// carry.
//
// The managed group stays. Karpenter's own chart pins the controller off
// Karpenter nodes with a required nodeAffinity on karpenter.sh/nodepool
// DoesNotExist, so a node the controller did not make has to exist for the
// controller to run at all.
//
// This file is the AWS half: the two identities, the interruption queue and the
// tag Karpenter discovers its network by. The NodePools and EC2NodeClasses are
// Kubernetes objects and live in helm/charts/karpenter-pools, rendered from the
// resolver's values.
//
// The controller policy below is AWS's published one for the pinned provider
// release, read from
// website/content/en/docs/getting-started/getting-started-with-karpenter/cloudformation.yaml
// at v1.14.1, with the instance-profile lifecycle statements dropped: the
// profile is created here, so the controller never creates, tags or deletes one.
//
// That published policy carries no KMS statement, because it has no idea which
// key a caller's EC2NodeClass might encrypt volumes with. karpenter_kms below
// supplies that separately, against this deployment's own key.

locals {
  // The one tag both selector terms match on. The value is the cluster name so
  // two clusters sharing a VPC cannot take each other's subnets.
  karpenter_discovery_key = "karpenter.sh/discovery"

  // Karpenter reads one queue and decides from the detail-type which event it
  // is holding. AWS Health carries scheduled retirement and degradation; the
  // other three are EC2's.
  karpenter_events = {
    spot-interruption     = { source = "aws.ec2", detail_type = "EC2 Spot Instance Interruption Warning" }
    rebalance             = { source = "aws.ec2", detail_type = "EC2 Instance Rebalance Recommendation" }
    instance-state-change = { source = "aws.ec2", detail_type = "EC2 Instance State-change Notification" }
    health                = { source = "aws.health", detail_type = "AWS Health Event" }
  }
}

// ---------------------------------------------------------------------------
// Discovery -- what the EC2NodeClass selector terms match
// ---------------------------------------------------------------------------

// EKS creates the cluster security group itself, so it is not a resource here
// and aws_ec2_tag is the way to add a tag to it. The same call against a subnet
// would fight aws_subnet's own tag management, which is why the subnets carry
// the tag in vpc.tf instead.
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

  // An interruption notice is worthless once the instance is gone, so a message
  // older than five minutes is deleted rather than replayed at a controller
  // that has just restarted.
  message_retention_seconds = 300

  // SQS-managed encryption rather than the deployment CMK: the messages are
  // instance ids and event types, and a CMK here would need an EventBridge
  // grant in the key policy to buy nothing.
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
      // The two service principals EventBridge delivers to a queue as. Fixed
      // by AWS.
      identifiers = ["events.amazonaws.com", "sqs.amazonaws.com"]
    }

    // Without this, any account's EventBridge rule that learns this queue's
    // ARN can enqueue to it -- a forged spot-interruption message naming a
    // real instance id would make Karpenter drain a node on a false alarm.
    // AWS's own published CloudFormation for Karpenter carries no such
    // condition (this queue policy matches it otherwise statement for
    // statement), so this is a deliberate tightening beyond upstream parity.
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

// Without these rules the queue is created, encrypted, granted and never
// written to -- which is what dfe-core shipped, and why its spot alerts could
// not fire.
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

  // Karpenter matches this target id in its own logs, so it is the upstream
  // spelling rather than a name of ours.
  target_id = "KarpenterInterruptionQueueTarget"
}

// ---------------------------------------------------------------------------
// Node identity -- a role of its own, so the controller's PassRole reaches
// exactly one role; it carries the same policy set as eks.tf's managed-group
// role on purpose, not by drift.
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
    // PullOnly, not ReadOnly: a node pulls images and never describes a
    // repository.
    "AmazonEC2ContainerRegistryPullOnly",
    // The nodes are in private subnets with no inbound path, so Session Manager
    // is how an operator reaches one.
    "AmazonSSMManagedInstanceCore",
  ])

  role       = aws_iam_role.karpenter_node.name
  policy_arn = "${local.managed_policy_prefix}/${each.value}"
}

// Karpenter is given a profile rather than a role, so it never needs
// iam:CreateInstanceProfile -- and a private cluster with no route to the IAM
// endpoint can still launch nodes.
resource "aws_iam_instance_profile" "karpenter_node" {
  name = "${var.name}-karpenter-node"
  path = var.iam_path
  role = aws_iam_role.karpenter_node.name
}

// authentication_mode is API, so the aws-auth ConfigMap is read by nobody and
// this entry is the only thing that lets a Karpenter node register. A managed
// node group gets one from EKS; a self-managed node does not.
resource "aws_eks_access_entry" "karpenter_node" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_iam_role.karpenter_node.arn

  // EC2_LINUX is what grants system:nodes and the node username; EKS fills both
  // in, and neither kubernetes_groups nor an access policy may be set with it.
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

    // Tagging is allowed only as part of the call that creates the thing, so a
    // stolen credential cannot relabel an existing instance into the cluster.
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

// RunInstances and CreateFleet are called under this role's credentials, so it
// is this role -- never the node role the instance itself assumes -- that EC2
// checks for KMS permission before it will attach an encrypted root volume.
// Without this grant every launch fails Client.InvalidKMSKey.InvalidState and
// the instance is terminated within seconds of joining.
//
// Built with jsonencode() directly rather than a data "aws_iam_policy_document"
// -- the same choice kms.tf and object-store.tf explain: a mocked
// aws_iam_policy_document's .json always returns the mock's fixed default
// regardless of the statement blocks, so a module test could not assert this
// policy carries the right actions and conditions.
locals {
  karpenter_kms_policy_statements = [
    {
      // The action list AWS documents for a service that launches EC2
      // instances with a customer managed key
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
      Resource = aws_kms_key.this.arn
      // The controller has no reason to touch this key outside a node launch,
      // so the grant is usable only when EC2 is the caller on its behalf.
      Condition = {
        StringEquals = {
          "kms:ViaService" = "ec2.${var.provision.region}.amazonaws.com"
        }
      }
    },
    {
      // CreateGrant is what lets the controller delegate a subset of its own
      // key permissions to EC2 for the life of the instance being launched.
      Sid      = "AllowEBSEncryptionGrants"
      Effect   = "Allow"
      Action   = "kms:CreateGrant"
      Resource = aws_kms_key.this.arn
      Condition = {
        StringEquals = {
          "kms:ViaService" = "ec2.${var.provision.region}.amazonaws.com"
        }
        // Restricts the grant to one an AWS service creates for itself,
        // matching the condition AWS's own key-policy documentation requires
        // on CreateGrant.
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

    // One role, the one the instance profile above carries. The controller
    // cannot hand a node any other identity in the account.
    resources = [aws_iam_role.karpenter_node.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      // Both EC2 principals AWS documents, because the China partitions use the
      // second one.
      values = ["ec2.amazonaws.com", "ec2.amazonaws.com.cn"]
    }
  }

  statement {
    sid     = "AllowInstanceProfileReadActions"
    effect  = "Allow"
    actions = ["iam:GetInstanceProfile"]

    // Read only: the EC2NodeClass reports InstanceProfileReady from this call,
    // and the profile itself is this module's to create and delete.
    resources = [aws_iam_instance_profile.karpenter_node.arn]
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

    // Where AWS publishes the EKS-optimised AMI ids the al2023 alias resolves
    // through. AWS owns the parameters, so the ARN carries no account.
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

  // Namespace and service account the chart is installed under in
  // argocd/appsets/layer1-addons.yaml. The chart's default name is generated
  // from the release, so the appset names it rather than leaving it to drift.
  namespace       = "kube-system"
  service_account = "karpenter"

  role_arn = aws_iam_role.karpenter.arn
}
