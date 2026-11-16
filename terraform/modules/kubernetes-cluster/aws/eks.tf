// Vanilla EKS with managed node groups, as raw resources rather than
// terraform-aws-modules/eks. See docs/deployment/aws-configuration.md.

data "aws_partition" "current" {}

// The principal running this apply, named in the cluster-admin grant below.
data "aws_caller_identity" "current" {}

// CreateAccessEntry rejects an assumed role's STS session ARN, and a bare role name too: an
// SSO role's ARN carries a path no regex can rebuild. This resolves the session back to the
// IAM role that issued it, path included (a plain IAM user returns itself).
data "aws_iam_session_context" "current" {
  arn = data.aws_caller_identity.current.arn
}

locals {
  // Only the partition varies in an AWS-managed policy ARN, so it is read.
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
  name                 = "${var.name}-cluster"
  path                 = var.iam_path
  assume_role_policy   = data.aws_iam_policy_document.eks_assume.json
  permissions_boundary = var.permissions_boundary
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "${local.managed_policy_prefix}/AmazonEKSClusterPolicy"
}

// EKS exports control-plane logs to CloudWatch only, and the group it auto-creates never
// expires, so the group is created here. No stream enabled means no group.
resource "aws_cloudwatch_log_group" "cluster" {
  count = length(var.telemetry.eks_log_types) > 0 ? 1 : 0

  name              = "/aws/eks/${var.name}/cluster"
  retention_in_days = var.telemetry.sink == "otel" ? var.telemetry.forced_retention_days : var.telemetry.retention_days
}

// Keeps an existing group from being deleted and recreated, which would drop its audit history.
moved {
  from = aws_cloudwatch_log_group.cluster
  to   = aws_cloudwatch_log_group.cluster[0]
}

resource "aws_eks_cluster" "this" {
  name     = var.name
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  enabled_cluster_log_types = var.telemetry.eks_log_types

  vpc_config {
    // Private subnets only. The load balancer controller finds the public ones by tag.
    subnet_ids = [for s in aws_subnet.private : s.id]

    endpoint_private_access = true
    endpoint_public_access  = var.endpoint.public

    // null, not an empty list, when the public endpoint is off: the API rejects an empty one.
    public_access_cidrs = var.endpoint.public ? var.endpoint.allowed_cidrs : null
  }

  access_config {
    authentication_mode = "API"
    // false: the true default grants an unnamed access entry to whoever ran the apply.
    // aws_eks_access_entry.creator below is the same grant, visible in a plan.
    bootstrap_cluster_creator_admin_permissions = false
  }

  encryption_config {
    resources = ["secrets"]

    provider {
      key_arn = local.kms_key_arn
    }
  }

  // The log group must exist before the log types are enabled, or EKS auto-creates one that
  // never expires. The zone teardown is listed so the destroy takes the cluster down first:
  // otherwise external-dns can republish a record mid-cleanup and DeleteHostedZone fails with
  // HostedZoneNotEmpty, which a re-run clears and so reads as a flaky destroy.
  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_iam_role_policy.cluster_kms,
    aws_cloudwatch_log_group.cluster,
    terraform_data.private_zone_teardown,
  ]
}

// The explicit form of bootstrap_cluster_creator_admin_permissions: cluster-admin for the
// deploying principal, visible in a plan.
resource "aws_eks_access_entry" "creator" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = data.aws_iam_session_context.current.issuer_arn

  type = "STANDARD"
}

resource "aws_eks_access_policy_association" "creator_admin" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = data.aws_iam_session_context.current.issuer_arn
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
      // The EC2 service principal. Fixed by AWS.
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "nodes" {
  name                 = "${var.name}-nodes"
  path                 = var.iam_path
  assume_role_policy   = data.aws_iam_policy_document.ec2_assume.json
  permissions_boundary = var.permissions_boundary
}

resource "aws_iam_role_policy_attachment" "nodes" {
  for_each = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEKS_CNI_Policy",
    // PullOnly, not ReadOnly: a node never describes a repository.
    "AmazonEC2ContainerRegistryPullOnly",
    // Session Manager is how an operator reaches a node in a private subnet.
    "AmazonSSMManagedInstanceCore",
  ])
  // The same policy set as the Karpenter node role in karpenter.tf.

  role       = aws_iam_role.nodes.name
  policy_arn = "${local.managed_policy_prefix}/${each.value}"
}

locals {
  // A launch template is the only way a node carries run tags, runs a pre-kubelet script or
  // assembles its instance store. With none of the three there is none, so no node group changes.
  node_launch_template = length(var.controller_tags) > 0 || var.node_user_data != ""

  // The nodeadm localStorage strategy each pool's shape asks for, matching its Karpenter pool's instanceStorePolicy.
  node_local_storage = {
    for name, pool in var.node_pools : name => var.resolved_shapes[pool.shape_ref].instance_store_policy
    if try(var.resolved_shapes[pool.shape_ref].instance_store_policy, null) != null
  }

  node_launch_templates = {
    for name, pool in var.node_pools : name => pool
    if local.node_launch_template || contains(keys(local.node_local_storage), name)
  }

  // EKS merges a managed group's user data with its own NodeConfig only in MIME multi-part form.
  node_user_data_boundary = "==DFE-NODE=="
  node_script_part = var.node_user_data == "" ? [] : [
    "--${local.node_user_data_boundary}",
    "Content-Type: text/x-shellscript; charset=\"us-ascii\"",
    "",
    chomp(var.node_user_data),
    "",
  ]
  node_user_data_mime = {
    for name in keys(local.node_launch_templates) : name => join("\n", concat(
      [
        "MIME-Version: 1.0",
        "Content-Type: multipart/mixed; boundary=\"${local.node_user_data_boundary}\"",
        "",
      ],
      contains(keys(local.node_local_storage), name) ? [
        "--${local.node_user_data_boundary}",
        "Content-Type: application/node.eks.aws",
        "",
        "---",
        "apiVersion: node.eks.aws/v1alpha1",
        "kind: NodeConfig",
        "spec:",
        "  instance:",
        "    localStorage:",
        "      strategy: ${local.node_local_storage[name]}",
        "",
      ] : [],
      local.node_script_part,
      ["--${local.node_user_data_boundary}--", ""],
    ))
    if var.node_user_data != "" || contains(keys(local.node_local_storage), name)
  }
}

resource "aws_launch_template" "nodes" {
  for_each = local.node_launch_templates

  name_prefix = "${var.name}-${each.key}-"
  description = "Node group ${each.key}'s root volume, metadata options, run tags, node script and instance store."

  user_data = contains(keys(local.node_user_data_mime), each.key) ? base64encode(local.node_user_data_mime[each.key]) : null

  // EKS refuses disk_size beside a launch template, so the root volume is sized here (/dev/xvda is AL2023's root).
  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_size           = each.value.disk_gb
      volume_type           = var.resolved_shapes[each.value.shape_ref].root_volume.type
      delete_on_termination = true
    }
  }

  // IMDSv2 with hop limit 1, like the Karpenter nodes: hop limit 2 lets a pod reach the node role.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  dynamic "tag_specifications" {
    for_each = length(var.controller_tags) > 0 ? ["instance", "volume"] : []

    content {
      resource_type = tag_specifications.value
      tags          = var.controller_tags
    }
  }
}

resource "aws_eks_node_group" "this" {
  for_each = var.node_pools

  cluster_name    = aws_eks_cluster.this.name
  node_group_name = each.key
  node_role_arn   = aws_iam_role.nodes.arn

  subnet_ids = [for s in aws_subnet.private : s.id]

  // The arch is checked against the resolved shape in the precondition below, not inferred from this family.
  ami_type = var.node_image.family

  // A dated release moves only on a resolve and an apply. Null keeps the release the group runs.
  release_version = var.node_image.version

  instance_types = var.resolved_shapes[each.value.shape_ref].instance_types
  capacity_type  = each.value.capacity_type
  disk_size      = contains(keys(local.node_launch_templates), each.key) ? null : each.value.disk_gb
  labels         = each.value.labels

  // Adding a launch template to a group that has none replaces the group, so a pool with no run
  // tags, script or instance store gets no block.
  dynamic "launch_template" {
    for_each = contains(keys(local.node_launch_templates), each.key) ? [aws_launch_template.nodes[each.key]] : []

    content {
      id      = launch_template.value.id
      version = launch_template.value.latest_version
    }
  }

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
    // One at a time: replacing several nodes of a stateful pool can take a quorum with it.
    max_unavailable = 1
  }

  // vpc-cni is created before the nodes and destroyed after them, so the CNI
  // outlives every node holding one of its network interfaces.
  depends_on = [aws_iam_role_policy_attachment.nodes, aws_eks_addon.vpc_cni]

  lifecycle {
    precondition {
      condition     = var.resolved_shapes[each.value.shape_ref].arch == "arm64"
      error_message = "Node pool '${each.key}' resolves to ${var.resolved_shapes[each.value.shape_ref].arch}, but the ${var.node_image.family} image this module launches is arm64 only."
    }
  }
}

// ---------------------------------------------------------------------------
// Add-ons -- the AWS components that replace the in-cluster ones
// ---------------------------------------------------------------------------

locals {
  // EKS API names. aws-ebs-csi-driver backs the gp3 StorageClass, and eks-pod-identity-agent is
  // what lets the roles below work with no credential in a pod.
  addons = ["coredns", "kube-proxy", "vpc-cni", "eks-pod-identity-agent", "aws-ebs-csi-driver"]

  // vpc-cni takes enableNetworkPolicy as the STRING "true", not a boolean. Without it nothing
  // enforces a NetworkPolicy object (helm/charts/network-policies).
  //
  // controller_tags reaches what these add-ons create themselves, which default_tags never sees:
  // ADDITIONAL_ENI_TAGS on pod network interfaces and extraVolumeTags on CSI volumes.
  addon_configuration = {
    "vpc-cni" = length(var.controller_tags) == 0 ? jsonencode({ enableNetworkPolicy = "true" }) : jsonencode({
      enableNetworkPolicy = "true"
      env                 = { ADDITIONAL_ENI_TAGS = jsonencode(var.controller_tags) }
    })
    "aws-ebs-csi-driver" = length(var.controller_tags) == 0 ? null : jsonencode({
      controller = { extraVolumeTags = var.controller_tags }
    })
  }
}

locals {
  // kube-proxy is built per Kubernetes minor, so its version names the minor the pins are for.
  kube_proxy_minor = try(regex("^v([0-9]+\\.[0-9]+)\\.", lookup(var.addon_versions, "kube-proxy", ""))[0], "")
}

// Its own resource for its order: created before the node groups and destroyed after them. A
// DaemonSet, so it needs no node to become ACTIVE.
resource "aws_eks_addon" "vpc_cni" {
  depends_on = [terraform_data.cni_eni_teardown]

  cluster_name  = aws_eks_cluster.this.name
  addon_name    = "vpc-cni"
  addon_version = lookup(var.addon_versions, "vpc-cni", null)

  configuration_values = local.addon_configuration["vpc-cni"]

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  lifecycle {
    precondition {
      condition     = contains(keys(var.addon_versions), "vpc-cni")
      error_message = "addon_versions names no version for vpc-cni. Give every add-on one, from `aws eks describe-addon-versions --kubernetes-version ${var.kubernetes_version} --addon-name vpc-cni`."
    }
  }
}

// Keeps an existing vpc-cni add-on from being deleted and recreated.
moved {
  from = aws_eks_addon.this["vpc-cni"]
  to   = aws_eks_addon.vpc_cni
}

// Deletes pod network interfaces the VPC CNI leaked, once the node groups and vpc-cni are gone
// and before the cluster is. A leaked `available` one holds the cluster security group and
// fails the VPC delete (docs/deployment/aws-teardown.md).
//
// The filter is the CNI's own leaked-interface one (amazon-vpc-cni-k8s pkg/awsutils/awsutils.go,
// getLeakedENIs). Its cluster-name tag is left out because CLUSTER_NAME may be unset, and this
// VPC holds one cluster.
//
// No on_failure = continue: a stuck interface fails the destroy here, not later as the VPC's
// DependencyViolation.
resource "terraform_data" "cni_eni_teardown" {
  // A dependent is destroyed first, so this runs before the cluster is deleted.
  depends_on = [aws_eks_cluster.this]

  // A destroy-time command may reference only self (dns.tf).
  input = {
    vpc_id = aws_vpc.this.id
    region = var.provision.region
  }

  // A replaced VPC is swept by the old instance, before the old cluster goes.
  triggers_replace = [aws_vpc.this.id]

  // Retries while an interface is still detaching, then fails.
  provisioner "local-exec" {
    when = destroy

    command = <<-EOT
      set -eu
      vpc="${self.output.vpc_id}"
      region="${self.output.region}"
      cni_enis() {
        ids="$(aws ec2 describe-network-interfaces --region "$region" --output text \
          --filters "Name=vpc-id,Values=$vpc" \
            "Name=tag-key,Values=node.k8s.amazonaws.com/instance_id" "$1" \
          --query 'NetworkInterfaces[].NetworkInterfaceId')" || return 1
        for id in $ids; do
          case "$id" in eni-*) printf '%s ' "$id" ;; esac
        done
      }
      attempt=1
      while :; do
        available="$(cni_enis Name=status,Values=available)"
        for eni in $available; do
          if out="$(aws ec2 delete-network-interface --region "$region" --network-interface-id "$eni" 2>&1)"; then
            echo "deleted leaked CNI network interface $eni"
          else
            case "$out" in
              *InvalidNetworkInterfaceID.NotFound*) ;;
              *) echo "cannot delete $eni yet: $out" >&2 ;;
            esac
          fi
        done
        available="$(cni_enis Name=status,Values=available)"
        detaching="$(cni_enis Name=attachment.status,Values=detaching)"
        if [ -z "$available$detaching" ]; then
          exit 0
        fi
        if [ "$attempt" -ge 12 ]; then
          echo "CNI network interfaces still in $vpc after $attempt rounds: $available$detaching" >&2
          exit 1
        fi
        attempt=$((attempt + 1))
        sleep 10
      done
    EOT
  }
}

// Pinned, never resolved: a version EKS publishes after the plan cannot reach a deploy unreviewed.
resource "aws_eks_addon" "this" {
  for_each = setsubtract(local.addons, ["vpc-cni"])

  cluster_name  = aws_eks_cluster.this.name
  addon_name    = each.key
  addon_version = lookup(var.addon_versions, each.key, null)

  configuration_values = lookup(local.addon_configuration, each.key, null)

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.this]

  lifecycle {
    precondition {
      condition     = contains(keys(var.addon_versions), each.key)
      error_message = "addon_versions names no version for ${each.key}. Give every add-on one, from `aws eks describe-addon-versions --kubernetes-version ${var.kubernetes_version} --addon-name ${each.key}`."
    }

    precondition {
      condition     = each.key != "kube-proxy" || local.kube_proxy_minor == var.kubernetes_version
      error_message = "addon_versions pins add-ons built for Kubernetes ${local.kube_proxy_minor == "" ? "an unreadable minor" : local.kube_proxy_minor} (kube-proxy ${lookup(var.addon_versions, "kube-proxy", "unset")}), but the cluster runs ${var.kubernetes_version}. Set every addon_versions entry to what `aws eks describe-addon-versions --kubernetes-version ${var.kubernetes_version}` lists."
    }
  }
}

// ---------------------------------------------------------------------------
// Pod Identity -- the EKS auth service is the whole trust policy, where IRSA needs an OIDC
// provider and a subject condition per service account.
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
  name                 = "${var.name}-ebs-csi"
  path                 = var.iam_path
  assume_role_policy   = data.aws_iam_policy_document.pod_identity_trust.json
  permissions_boundary = var.permissions_boundary
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
// Toolbox operator -- read-only EKS access for whoever is behind the bastion's tunnel. No
// entry unless toolbox_operator_role_arn is set. It is a SECOND entry, never a wider scope on
// the creator's, and the toolbox instance gets none: the kubectl identity is the operator's own
// role (terraform/modules/toolbox/aws/CONTRACT.md, "EKS access").
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
