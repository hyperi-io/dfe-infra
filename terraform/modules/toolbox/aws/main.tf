// The on-demand SSM-managed troubleshooting instance. Always instantiated by
// the aws root (no `count` on the module block); `enabled` gates every
// resource here via a per-resource `count`/`for_each`, EXCEPT the session-log
// bucket, which has no such gate -- see variables.tf's header comment and
// CONTRACT.md for why the evidence has to outlive the instance it records.

data "aws_partition" "current" {}

locals {
  instance_tags = merge(var.tags, {
    Name                      = var.name
    "dfe.hyperi.io/component" = "toolbox"
  })

  // 443 unconditionally: the SSM control channel, the ECR API and the EKS
  // API all speak it, and none sit behind a VPC interface endpoint here --
  // this module trades that endpoint cost (roughly one more instance's worth
  // of spend per AZ) for the internet-egress residual CONTRACT.md #5 states
  // plainly rather than hides. It has to reach the internet via NAT, so it is
  // 0.0.0.0/0, matching managed-kafka/msk's own equivalent gap (main.tf).
  control_egress_port = 443

  // The Kubernetes API's own port, which happens to equal the egress constant
  // above and must not move with it: one is what this instance dials out on,
  // the other is what a group it does not own has to admit it on.
  kubernetes_api_port = 443

  // Every OTHER target port -- Kafka, ClickHouse, a future Keeper -- and every
  // managed-backend port in var.backend_egress gets one egress rule, aimed by
  // its own scope: the VPC CIDR for an address inside the VPC, 0.0.0.0/0 for
  // one the deployer brought from outside it. Keyed on the scope AND the port,
  // so two entries sharing a port on opposite sides of the VPC boundary get a
  // rule each rather than collapsing into whichever one the key happened to keep.
  target_egress = {
    for pair in distinct(concat(
      [for t in var.targets : { scope = t.scope, port = t.port }],
      [for e in var.backend_egress : { scope = e.scope, port = e.port }],
    )) : "${pair.scope}-${pair.port}" => pair
    if pair.port != local.control_egress_port
  }
}

// ---------------------------------------------------------------------------
// Network -- NO ingress rule of any kind. SSM's control channel and every
// forward are initiated FROM the instance; nothing ever needs to reach it.
// ---------------------------------------------------------------------------

resource "aws_security_group" "this" {
  count = var.enabled ? 1 : 0

  name        = "${var.name}-toolbox"
  description = "Toolbox instance for ${var.name} (${var.env}) -- no ingress rule of any kind"
  vpc_id      = var.network.vpc_id

  tags = merge(var.tags, { Name = "${var.name}-toolbox" })
}

resource "aws_vpc_security_group_egress_rule" "control" {
  count = var.enabled ? 1 : 0

  security_group_id = aws_security_group.this[0].id

  cidr_ipv4   = "0.0.0.0/0"
  ip_protocol = "tcp"
  from_port   = local.control_egress_port
  to_port     = local.control_egress_port

  description = "SSM control channel, ECR API, EKS API -- none sit behind a VPC interface endpoint here"
}

resource "aws_vpc_security_group_egress_rule" "targets" {
  for_each = var.enabled ? local.target_egress : {}

  security_group_id = aws_security_group.this[0].id

  cidr_ipv4   = each.value.scope == "internet" ? "0.0.0.0/0" : var.network.cidr
  ip_protocol = "tcp"
  from_port   = each.value.port
  to_port     = each.value.port

  description = "Forward target or managed backend on ${each.value.port}, scope ${each.value.scope}"
}

// The tunnel's own UDP listeners -- the only non-TCP egress this module has.
// Scoped to the one address the edge module built, so it reaches the tunnel and
// nothing else on the internet, and absent entirely when there is no such
// address (variables.tf `tunnel`).
//
// Keyed on the PORTS and never on the address: the address is an Elastic IP that
// is unknown until apply, and a for_each that reads it refuses to plan at all on
// the run that creates it. The ports travel with the address, so an empty list
// is the same "no forwarder" case the address's own emptiness names.
resource "aws_vpc_security_group_egress_rule" "tunnel" {
  for_each = var.enabled && length(var.tunnel.ports) > 0 ? toset([for p in var.tunnel.ports : tostring(p)]) : toset([])

  security_group_id = aws_security_group.this[0].id

  cidr_ipv4   = "${var.tunnel.address}/32"
  ip_protocol = "udp"
  from_port   = tonumber(each.value)
  to_port     = tonumber(each.value)

  description = "Fleet tunnel listener ${each.value}/udp at ${var.tunnel.address}"
}

// The reach-back's own egress, aimed at the tunnel's CLIENT range rather than
// at one appliance, because the hub allocates a peer's tunnel address and no
// plan can know it. Absent when the admin class is off, and it carries no
// dependency on the tunnel address: this path routes the client range at the
// culvert pod instead of dialling the tunnel.
resource "aws_vpc_security_group_egress_rule" "tunnel_reach" {
  for_each = var.enabled && var.tunnel.client_cidr != "" ? toset([for p in var.tunnel.reach : tostring(p)]) : toset([])

  security_group_id = aws_security_group.this[0].id

  cidr_ipv4   = var.tunnel.client_cidr
  ip_protocol = "tcp"
  from_port   = tonumber(each.value)
  to_port     = tonumber(each.value)

  description = "Appliance ${each.value}/tcp through the fleet tunnel"
}

// The one rule this module puts on a security group it does not own. Egress on
// 443 is not enough to reach the Kubernetes API: the control plane's own group
// admits nodes and pods and nothing else, so the eks-api forward target times
// out against an open client side until the toolbox group is named here.
// Referenced by group id rather than by CIDR, so the grant names this instance
// rather than the whole VPC, and `bastion down` takes it away with the group.
// That group is shared: kubernetes-cluster/aws/karpenter.tf tags it for node
// discovery, so every Karpenter node attaches it and this rule reaches TCP/443
// on the nodes as well as the control-plane ENIs.
resource "aws_vpc_security_group_ingress_rule" "eks_api" {
  count = var.enabled && var.eks_cluster_security_group_id != "" ? 1 : 0

  security_group_id = var.eks_cluster_security_group_id

  referenced_security_group_id = aws_security_group.this[0].id
  ip_protocol                  = "tcp"
  from_port                    = local.kubernetes_api_port
  to_port                      = local.kubernetes_api_port

  description = "Kubernetes API from the ${var.name} toolbox instance"

  tags = merge(var.tags, { Name = "${var.name}-toolbox-eks-api" })
}

// ---------------------------------------------------------------------------
// Identity -- AmazonSSMManagedInstanceCore, ECR pull, and an inline policy
// scoped to exactly the session-log bucket prefix and the deployment key.
// No ReadOnlyAccess (that reaches the CloudTrail and MSK broker-log buckets
// too -- CONTRACT.md #4), no ec2:TerminateInstances, no ssm:DescribeSessions
// (CONTRACT.md #9 -- the idle timer needs neither).
// ---------------------------------------------------------------------------

resource "aws_iam_role" "this" {
  count = var.enabled ? 1 : 0

  name = "${var.name}-toolbox"
  path = var.iam_path

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })

  permissions_boundary = var.permissions_boundary

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  count = var.enabled ? 1 : 0

  role       = aws_iam_role.this[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "ecr_pull" {
  count = var.enabled ? 1 : 0

  role       = aws_iam_role.this[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly"
}

// The key's own policy delegates to IAM via its account-root statement
// (kubernetes-cluster/aws/kms.tf), so this inline role policy is sufficient
// on its own -- this module writes NO aws_kms_key_policy and needs no entry
// in the root's key_policy_grants either, because that mechanism exists only
// for an AWS SERVICE PRINCIPAL with no IAM identity to attach a policy to
// (CloudTrail, MSK's broker-log delivery); the caller here is this role.
resource "aws_iam_role_policy" "session_log" {
  count = var.enabled ? 1 : 0

  name = "${var.name}-toolbox-session-log"
  role = aws_iam_role.this[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "SessionLogEncryption"
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = var.kms_key_arn
      },
      {
        Sid      = "SessionLogWrite"
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${var.session_log_bucket_arn}/*"
      },
    ]
  })
}

resource "aws_iam_instance_profile" "this" {
  count = var.enabled ? 1 : 0

  name = "${var.name}-toolbox"
  path = var.iam_path
  role = aws_iam_role.this[0].name
}

// ---------------------------------------------------------------------------
// The instance
// ---------------------------------------------------------------------------

// The primary fingerprint of every key templates/user_data.sh.tftpl checks a
// download against. Trust anchors, not tunables: each moves only in a reviewed
// change, so they sit here beside the vendored keys rather than in versions.yaml.
locals {
  signing_keys = {
    kubernetes    = "DE15B14486CD377B9E876E1A234654DA9A296436" // isv:kubernetes OBS Project, signs the pkgs.k8s.io rpm repos
    opentofu      = "E3E6E43D84CB852EADB0051D0C0AF313E5FD9F80" // OpenTofu, signs the tofu rpm packages
    opentofu_repo = "F4AF70F66EAC4337EEECC97407D3DFCD4C61499F" // packages.opentofu.org, signs the tofu rpm repo metadata
    clickhouse    = "3A9EA1193A97B548BE1457D48919F6BD2B48D754" // ClickHouse Inc. Repositories Key, signs the rpm repo metadata
    helm          = "208DD36ED5BB3745A16743A4C7C6FBB5B91C1155" // helm release key, signs the release tarballs
    aws_cli       = "FB5DB77FD5C118B80511ADA8A6310ACC4672475C" // AWS CLI Team, keys/aws-cli.asc
    ssm_plugin    = "7959637124CE093AD501D47A2C4D4AFF6F6757EE" // AWS SSM Session Manager, keys/session-manager-plugin.asc
  }

  // EC2's own cap on user data, in bytes before base64 encoding.
  user_data_limit = 16384

  // Rendered only for an instance that exists, so a disabled toolbox needs no tool_versions.
  user_data = var.enabled ? templatefile("${path.module}/templates/user_data.sh.tftpl", {
    kubectl_version           = var.tool_versions["kubectl"]
    helm_version              = var.tool_versions["helm"]
    argocd_version            = var.tool_versions["argocd-cli"]
    clickhouse_client_version = var.tool_versions["clickhouse-client"]
    postgresql_major_version  = var.tool_versions["psql"]
    tofu_version              = var.tool_versions["tofu"]
    yq_version                = var.tool_versions["yq"]
    awscli_version            = var.tool_versions["aws-cli"]
    ssm_plugin_version        = var.tool_versions["aws-session-manager-plugin"]
    kafka_cli_version         = var.tool_versions["kafka-cli"]
    ttl_minutes               = var.ttl_minutes
    argocd_sha256             = var.tool_versions["argocd-cli-arm64-sha256"]
    yq_sha256                 = var.tool_versions["yq-arm64-sha256"]
    kafka_cli_signer_fpr      = split(":", var.tool_versions["kafka-cli-signer"])[1]
    kubernetes_key_fpr        = local.signing_keys.kubernetes
    opentofu_key_fpr          = local.signing_keys.opentofu
    opentofu_repo_key_fpr     = local.signing_keys.opentofu_repo
    clickhouse_key_fpr        = local.signing_keys.clickhouse
    helm_key_fpr              = local.signing_keys.helm
    awscli_key_fpr            = local.signing_keys.aws_cli
    awscli_key                = chomp(file("${path.module}/keys/aws-cli.asc"))
    ssm_plugin_key_fpr        = local.signing_keys.ssm_plugin
    ssm_plugin_key            = chomp(file("${path.module}/keys/session-manager-plugin.asc"))
  }) : ""
}

resource "aws_instance" "this" {
  count = var.enabled ? 1 : 0

  ami           = var.ami_id
  instance_type = var.instance_type
  subnet_id     = var.network.private_subnet_ids[0]

  vpc_security_group_ids = [aws_security_group.this[0].id]
  iam_instance_profile   = aws_iam_instance_profile.this[0].name

  // Private subnet already refuses map_public_ip_on_launch at the subnet
  // level (kubernetes-cluster/aws/vpc.tf); asserted again here by name.
  associate_public_ip_address = false

  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = var.root_volume.type
    volume_size           = var.root_volume.size_gib
    encrypted             = true
    kms_key_id            = var.kms_key_arn
    delete_on_termination = true
  }

  // No ec2:TerminateInstances anywhere in this module -- this setting is
  // the WHOLE self-terminate mechanism (CONTRACT.md #9). The idle timer in
  // user_data calls `shutdown -h now`; this is what turns that OS shutdown
  // into an actual termination, needing zero IAM.
  instance_initiated_shutdown_behavior = "terminate"

  user_data = local.user_data

  tags = local.instance_tags

  lifecycle {
    precondition {
      condition     = length(local.user_data) <= local.user_data_limit
      error_message = "the rendered user data is ${length(local.user_data)} bytes and EC2 refuses more than ${local.user_data_limit} -- trim templates/user_data.sh.tftpl."
    }
  }
}

// ---------------------------------------------------------------------------
// Session documents -- ONE per purpose. The shell document carries the
// account default's job (idle timeout, max duration, S3 logging, non-root
// runAs) so a caller never has to fall back to SSM-SessionManagerRunShell to
// get it; the forward documents fix host and port in the document body so a
// forward session can never be aimed anywhere else (CONTRACT.md #1, #2, #7).
// ---------------------------------------------------------------------------

resource "aws_ssm_document" "shell" {
  count = var.enabled ? 1 : 0

  name            = "${var.name}-toolbox-shell"
  document_type   = "Session"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "1.0"
    description   = "Interactive shell on the ${var.name} (${var.env}) toolbox -- fully logged, unlike a forward session (CONTRACT.md #6)."
    sessionType   = "Standard_Stream"
    inputs = {
      s3BucketName                = var.session_log_bucket
      s3KeyPrefix                 = "shell/"
      s3EncryptionEnabled         = true
      cloudWatchLogGroupName      = ""
      cloudWatchEncryptionEnabled = false
      cloudWatchStreamingEnabled  = false
      kmsKeyId                    = ""
      runAsEnabled                = true
      runAsDefaultUser            = "ssm-user"
      idleSessionTimeout          = tostring(var.session.idle_timeout_minutes)
      maxSessionDuration          = tostring(var.session.max_duration_minutes)
      shellProfile = {
        // tmpfs, not the root volume -- a shell command history is exactly
        // the kind of thing a debugging session should not leave behind on
        // disk between sessions (CONTRACT.md #3/P3-5).
        linux = "export HISTFILE=/dev/shm/.toolbox_history_$$"
      }
    }
  })

  tags = var.tags
}

resource "aws_ssm_document" "forward" {
  for_each = var.enabled ? var.targets : {}

  name            = "${var.name}-toolbox-forward-${each.key}"
  document_type   = "Session"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "1.0"
    description   = "Port-forward to the ${each.key} endpoint of ${var.name} (${var.env}) -- host and port fixed in this document, never caller-supplied (CONTRACT.md #7)."
    sessionType   = "Port"
    // ONLY localPortNumber is a parameter. There is no `host` or `portNumber`
    // parameter declared at all, so `--parameters host=...` against THIS
    // document name is rejected as an unknown parameter -- the fence is
    // structural, not an IAM condition on a value IAM has no key for.
    parameters = {
      localPortNumber = {
        type           = "String"
        description    = "Local port on the operator's machine"
        allowedPattern = "^([0-9]{1,5})?$"
      }
    }
    properties = {
      type            = "LocalPortForwarding"
      host            = each.value.host
      portNumber      = tostring(each.value.port)
      localPortNumber = "{{ localPortNumber }}"
    }
  })

  tags = var.tags
}
