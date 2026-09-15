// The on-demand SSM-managed troubleshooting instance. Always instantiated by
// the aws root (no `count` on the module block); `enabled` gates every
// resource here via a per-resource `count`/`for_each`, EXCEPT the session-log
// bucket, which has no such gate -- see variables.tf's header comment and
// CONTRACT.md for why the evidence has to outlive the instance it records.

data "aws_partition" "current" {}

// Always-latest alias, deliberately NOT pinned to a dated release the way
// karpenter-pools.amiAlias is (al2023@v20260827). That pin exists because a
// Karpenter node is long-lived and reconciles continuously, so "latest"
// there means silent mid-life drift with no version an operator can point
// at. This instance is created fresh on every `bastion up` and terminated on
// every `bastion down` -- there is no "running node whose image moved under
// it" failure mode to guard against, and an on-demand debugging box SHOULD
// carry whatever AL2023 currently ships. See CONTRACT.md #8/P3-2 for the
// full reasoning and the deliberate deviation from the Karpenter precedent.
data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

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

  // Every OTHER target port -- Kafka, ClickHouse, a future Keeper -- lives
  // INSIDE the VPC by construction (targets are only ever cluster-internal
  // endpoints the aws root computed from its own other modules), so egress
  // for them is scoped to the VPC CIDR, never 0.0.0.0/0.
  // for_each only accepts a set of strings, so the port set is stringified
  // here and turned back into a number at the one place that needs it.
  target_ports = toset([for t in var.targets : tostring(t.port) if t.port != local.control_egress_port])
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
  for_each = var.enabled ? local.target_ports : toset([])

  security_group_id = aws_security_group.this[0].id

  cidr_ipv4   = var.network.cidr
  ip_protocol = "tcp"
  from_port   = tonumber(each.value)
  to_port     = tonumber(each.value)

  description = "Forward target on ${each.value}, scoped to the VPC -- every named target lives inside it"
}

// The one rule this module puts on a security group it does not own. Egress on
// 443 is not enough to reach the Kubernetes API: the control plane's own group
// admits nodes and pods and nothing else, so the eks-api forward target times
// out against an open client side until the toolbox group is named here.
// Referenced by group id rather than by CIDR, so the grant names this instance
// rather than the whole VPC, and `bastion down` takes it away with the group.
resource "aws_vpc_security_group_ingress_rule" "eks_api" {
  count = var.enabled && var.eks_cluster_security_group_id != "" ? 1 : 0

  security_group_id = var.eks_cluster_security_group_id

  referenced_security_group_id = aws_security_group.this[0].id
  ip_protocol                  = "tcp"
  from_port                    = local.control_egress_port
  to_port                      = local.control_egress_port

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

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })

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
        Resource = "${aws_s3_bucket.session_logs.arn}/*"
      },
    ]
  })
}

resource "aws_iam_instance_profile" "this" {
  count = var.enabled ? 1 : 0

  name = "${var.name}-toolbox"
  role = aws_iam_role.this[0].name
}

// ---------------------------------------------------------------------------
// The instance
// ---------------------------------------------------------------------------

resource "aws_instance" "this" {
  count = var.enabled ? 1 : 0

  ami           = data.aws_ssm_parameter.al2023_arm64.value
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
    volume_type           = "gp3"
    volume_size           = 20
    encrypted             = true
    kms_key_id            = var.kms_key_arn
    delete_on_termination = true
  }

  // No ec2:TerminateInstances anywhere in this module -- this setting is
  // the WHOLE self-terminate mechanism (CONTRACT.md #9). The idle timer in
  // user_data calls `shutdown -h now`; this is what turns that OS shutdown
  // into an actual termination, needing zero IAM.
  instance_initiated_shutdown_behavior = "terminate"

  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    kubectl_version           = var.tool_versions["kubectl"]
    helm_version              = var.tool_versions["helm"]
    argocd_version            = var.tool_versions["argocd-cli"]
    clickhouse_client_version = var.tool_versions["clickhouse-client"]
    postgresql_major_version  = var.tool_versions["psql"]
    tofu_version              = var.tool_versions["tofu"]
    yq_version                = var.tool_versions["yq"]
    awscli_version            = var.tool_versions["aws-cli"]
    ssm_plugin_version        = var.tool_versions["aws-session-manager-plugin"]
    ttl_minutes               = var.ttl_minutes
  })

  tags = local.instance_tags
}

// ---------------------------------------------------------------------------
// Session logs -- NOT gated by `enabled`. Evidence of human access to a
// customer's private data plane, not service telemetry (CONTRACT.md #18), so
// it outlives every up/down cycle and carries its own retention -- never
// telemetry.retention_days. No Object Lock: this bucket is torn down with the
// deployment, and Object Lock fights that the same way it does on every other
// log bucket in this repo.
// ---------------------------------------------------------------------------

resource "aws_s3_bucket" "session_logs" {
  bucket        = "${var.name}-toolbox-session-logs"
  force_destroy = var.force_destroy_session_logs

  tags = merge(var.tags, { Name = "${var.name}-toolbox-session-logs" })
}

resource "aws_s3_bucket_public_access_block" "session_logs" {
  bucket = aws_s3_bucket.session_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "session_logs" {
  bucket = aws_s3_bucket.session_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }

    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "session_logs" {
  bucket = aws_s3_bucket.session_logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "session_logs" {
  bucket = aws_s3_bucket.session_logs.id

  rule {
    id     = "expire-session-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.session_log_retention_days
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
      s3BucketName                = aws_s3_bucket.session_logs.bucket
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
