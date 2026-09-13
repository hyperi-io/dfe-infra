// Vanilla EKS with managed node groups -- not RKE2 on EC2.
//
// Raw resources rather than terraform-aws-modules/eks, following the shape
// dfe-core's own EKS module proved, so the surface is exactly what is declared
// here.

data "aws_partition" "current" {}

// The principal running this apply, so the cluster-admin grant below names
// them explicitly rather than relying on EKS's own implicit "whoever created
// the cluster" rule.
data "aws_caller_identity" "current" {}

locals {
  // AWS-managed policies are addressed by name under the aws-managed account;
  // only the partition varies, which is why it is read rather than written.
  managed_policy_prefix = "arn:${data.aws_partition.current.partition}:iam::aws:policy"
}

// ---------------------------------------------------------------------------
// Control plane
// ---------------------------------------------------------------------------

data "aws_iam_policy_document" "eks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type = "Service"
      // The EKS service principal. Fixed by AWS.
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${var.name}-cluster"
  assume_role_policy = data.aws_iam_policy_document.eks_assume.json
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "${local.managed_policy_prefix}/AmazonEKSClusterPolicy"
}

// EKS delivers control-plane logs to CloudWatch and nowhere else -- there is
// no S3 or OTel export path for them, unlike MSK's broker logs -- so this is
// created explicitly rather than left to EKS's own auto-create-on-enable
// behaviour, which carries NO expiration at all. audit is the one stream kept
// on: it is the compliance-relevant one, and api/controllerManager/scheduler
// would be pure CloudWatch cost with nothing downstream to read them.
resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${var.name}/cluster"
  retention_in_days = var.telemetry.sink == "otel" ? 1 : var.telemetry.retention_days
}

resource "aws_eks_cluster" "this" {
  name     = var.name
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  enabled_cluster_log_types = ["audit"]

  vpc_config {
    // PRIVATE subnets only. The control-plane network interfaces belong on the
    // inside; the load balancer controller finds the public subnets by tag, not
    // from this list.
    subnet_ids = [for s in aws_subnet.private : s.id]

    endpoint_private_access = true
    endpoint_public_access  = var.endpoint.public

    // Omitted entirely when the public endpoint is off -- an empty list is
    // rejected by the API rather than read as "nobody".
    public_access_cidrs = var.endpoint.public ? var.endpoint.allowed_cidrs : null
  }

  access_config {
    authentication_mode = "API"
    // false, not the implicit grant: the true default writes an access entry
    // for whatever principal ran the apply, unnamed and unreviewable in any
    // plan. aws_eks_access_entry.creator below is the same grant, made explicit.
    bootstrap_cluster_creator_admin_permissions = false
  }

  encryption_config {
    resources = ["secrets"]

    provider {
      key_arn = aws_kms_key.this.arn
    }
  }

  // The log group has to exist before enabled_cluster_log_types takes effect,
  // or EKS auto-creates one of its own with no expiration set.
  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_iam_role_policy.cluster_kms,
    aws_cloudwatch_log_group.cluster,
  ]
}

// The explicit stand-in for bootstrap_cluster_creator_admin_permissions: the
// same cluster-admin grant EKS would otherwise hand the deploying principal
// implicitly, but declared here so it appears in a plan and survives being
// read back rather than being inferred from whoever happened to run apply.
resource "aws_eks_access_entry" "creator" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = data.aws_caller_identity.current.arn

  type = "STANDARD"
}

resource "aws_eks_access_policy_association" "creator_admin" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = data.aws_caller_identity.current.arn
  policy_arn    = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.creator]
}

// ---------------------------------------------------------------------------
// Nodes
// ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type = "Service"
      // The EC2 service principal, which is what a node instance profile
      // assumes. Fixed by AWS.
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "nodes" {
  name               = "${var.name}-nodes"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "nodes" {
  for_each = toset([
    // The three AWS-managed policies a managed node group is documented to
    // need: join the cluster, run the VPC CNI, pull from ECR.
    "AmazonEKSWorkerNodePolicy",
    "AmazonEKS_CNI_Policy",
    // PullOnly, not ReadOnly: a node pulls images and never describes a
    // repository.
    "AmazonEC2ContainerRegistryPullOnly",
    // The nodes are in private subnets with no inbound path, so Session Manager
    // is how an operator reaches one.
    "AmazonSSMManagedInstanceCore",
  ])
  // Deliberately the same policy set as the Karpenter node role in karpenter.tf
  // -- one node role should not have a capability its sibling lacks.

  role       = aws_iam_role.nodes.name
  policy_arn = "${local.managed_policy_prefix}/${each.value}"
}

resource "aws_eks_node_group" "this" {
  for_each = var.node_pools

  cluster_name    = aws_eks_cluster.this.name
  node_group_name = each.key
  node_role_arn   = aws_iam_role.nodes.arn

  // Nodes are private. Anything they need to reach outward goes through the
  // NAT gateways.
  subnet_ids = [for s in aws_subnet.private : s.id]

  // ARM everywhere in the cloud. AL2023_ARM_64_STANDARD is the EKS API's own
  // name for the arm64 Amazon Linux 2023 image; the arch itself is asserted
  // against the resolved shape below rather than assumed from this string.
  ami_type = "AL2023_ARM_64_STANDARD"

  instance_types = var.resolved_shapes[each.value.shape_ref].instance_types
  capacity_type  = each.value.capacity_type
  disk_size      = each.value.disk_gb
  labels         = each.value.labels

  scaling_config {
    min_size     = each.value.min_size
    max_size     = each.value.max_size
    desired_size = each.value.desired_size
  }

  dynamic "taint" {
    for_each = each.value.taints

    content {
      key    = taint.value.key
      value  = taint.value.value
      effect = taint.value.effect
    }
  }

  update_config {
    // One node at a time. A pool of two or three carries stateful pods, and
    // replacing more than one at once can take a quorum with it.
    max_unavailable = 1
  }

  depends_on = [aws_iam_role_policy_attachment.nodes]

  lifecycle {
    precondition {
      condition     = var.resolved_shapes[each.value.shape_ref].arch == "arm64"
      error_message = "Node pool '${each.key}' resolves to ${var.resolved_shapes[each.value.shape_ref].arch}, but the AL2023_ARM_64_STANDARD image this module launches is arm64 only."
    }
  }
}

// ---------------------------------------------------------------------------
// Add-ons -- the AWS components that replace the in-cluster ones
// ---------------------------------------------------------------------------

locals {
  // Add-on names are EKS API identifiers. aws-ebs-csi-driver is what makes a
  // gp3 StorageClass possible, which is why DFE's storage_class becomes gp3 on
  // this cloud; eks-pod-identity-agent is what makes every role below work
  // without a credential in a pod.
  addons = ["coredns", "kube-proxy", "vpc-cni", "eks-pod-identity-agent", "aws-ebs-csi-driver"]
}

// Resolve each add-on's version for this cluster version rather than pinning
// one, so an add-on pin cannot rot against a cluster upgrade.
data "aws_eks_addon_version" "this" {
  for_each = toset(local.addons)

  addon_name         = each.key
  kubernetes_version = aws_eks_cluster.this.version
  most_recent        = true
}

resource "aws_eks_addon" "this" {
  for_each = toset(local.addons)

  cluster_name  = aws_eks_cluster.this.name
  addon_name    = each.key
  addon_version = data.aws_eks_addon_version.this[each.key].version

  // vpc-cni's own configuration schema (aws eks describe-addon-configuration)
  // takes enableNetworkPolicy as a top-level STRING "true"/"false", not a JSON
  // boolean -- confirmed against AWS's own worked example
  // (https://aws.amazon.com/blogs/containers/amazon-vpc-cni-now-supports-kubernetes-network-policies/).
  // With no config, network-policies' whole chart is decorative on this
  // cluster: the add-on is created with no configuration_values at all
  // otherwise, so nothing enforces a NetworkPolicy object. See
  // helm/charts/network-policies and the toolbox pod's own fence.
  configuration_values = each.key == "vpc-cni" ? jsonencode({ enableNetworkPolicy = "true" }) : null

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.this]
}

// ---------------------------------------------------------------------------
// Pod Identity -- the principal is the EKS auth service, so this is the whole
// trust policy. IRSA would need an OIDC provider plus a subject condition per
// service account.
// ---------------------------------------------------------------------------

data "aws_iam_policy_document" "pod_identity_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type = "Service"
      // The EKS Pod Identity service principal. Fixed by AWS.
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${var.name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "${local.managed_policy_prefix}/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_eks_pod_identity_association" "ebs_csi" {
  cluster_name = aws_eks_cluster.this.name

  // Namespace and service account the EBS CSI add-on ships with.
  namespace       = "kube-system"
  service_account = "ebs-csi-controller-sa"

  role_arn = aws_iam_role.ebs_csi.arn
}

// ---------------------------------------------------------------------------
// Toolbox operator -- read-only EKS access for whoever is behind the
// on-demand bastion's tunnel. Empty var.toolbox_operator_role_arn means no
// entry at all: the caller (the aws root) passes it only when the toolbox is
// enabled. This is deliberately a SECOND access entry, never a wider scope on
// aws_eks_access_entry.creator above -- a troubleshooting session should not
// default to the identity that can rewrite the cluster. The toolbox EC2
// instance/pod themselves get no access entry anywhere in this module or the
// toolbox module -- the tunnel terminates TLS at the operator's laptop, so
// the kubectl identity is the operator's own role, never the instance's
// (terraform/modules/toolbox/aws/CONTRACT.md, "EKS access").
// ---------------------------------------------------------------------------

resource "aws_eks_access_entry" "toolbox_operator" {
  count = var.toolbox_operator_role_arn != "" ? 1 : 0

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = var.toolbox_operator_role_arn

  type = "STANDARD"
}

resource "aws_eks_access_policy_association" "toolbox_operator_view" {
  count = var.toolbox_operator_role_arn != "" ? 1 : 0

  cluster_name  = aws_eks_cluster.this.name
  principal_arn = var.toolbox_operator_role_arn
  policy_arn    = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.toolbox_operator]
}
