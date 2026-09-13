// MSK Provisioned with EXPRESS brokers, on the cluster module's private
// subnets. Express rather than Standard because MSK cannot convert a Standard
// cluster to Express afterwards -- choosing Standard now to move later IS the
// rebuild the choice is trying to avoid. Express also manages its own storage,
// so nothing here sizes a volume.
//
// Authentication is BOTH SASL/SCRAM and SASL/IAM: DFE's apps stay on SCRAM, and
// IAM exists for the in-cluster bootstrap Job alone, which is what lets the Job
// create the first Kafka ACL on a cluster that grants nothing by default.

data "aws_partition" "current" {}

data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

locals {
  // MSK takes one instance type for the whole cluster, so the resolver's
  // best-first list collapses to its head here; the rest are the fallbacks a
  // node group would use.
  broker_instance_type = var.resolved_shapes[var.broker_shape_ref].instance_types[0]

  // An empty client_cidrs means the whole VPC -- the private network the DFE
  // pods and the bootstrap Job already sit on.
  client_cidrs = length(var.client_cidrs) > 0 ? var.client_cidrs : [var.network.cidr]

  // The two broker ports MSK fixes: 9096 is SASL/SCRAM over TLS, 9098 is
  // SASL/IAM. Both are protocol constants, not choices.
  broker_ports = {
    scram = 9096
    iam   = 9098
  }

  // One ingress rule per port per allowed range.
  ingress_rules = {
    for pair in setproduct(keys(local.broker_ports), local.client_cidrs) :
    "${pair[0]}-${pair[1]}" => { port = local.broker_ports[pair[0]], cidr = pair[1] }
  }
}

// ---------------------------------------------------------------------------
// Who may reach the brokers
// ---------------------------------------------------------------------------

resource "aws_security_group" "brokers" {
  name        = "${var.name}-msk"
  description = "MSK broker network interfaces for ${var.name} (${var.env})"
  vpc_id      = var.network.vpc_id

  tags = { Name = "${var.name}-msk" }
}

resource "aws_vpc_security_group_ingress_rule" "brokers" {
  for_each = local.ingress_rules

  security_group_id = aws_security_group.brokers.id

  cidr_ipv4   = each.value.cidr
  ip_protocol = "tcp"
  from_port   = each.value.port
  to_port     = each.value.port

  description = "Kafka clients on ${each.value.port}"
}

// A security group created here starts with NO egress rule, which denies all
// outbound -- and the broker interfaces reach Secrets Manager, KMS and
// CloudWatch Logs from the private subnets.
resource "aws_vpc_security_group_egress_rule" "brokers" {
  security_group_id = aws_security_group.brokers.id

  cidr_ipv4   = "0.0.0.0/0"
  ip_protocol = "-1"

  description = "Broker egress to the AWS services MSK depends on"
}

// ---------------------------------------------------------------------------
// The broker tuning block, reduced to what Express accepts
// ---------------------------------------------------------------------------

// Express holds the thread pools, the replication factor, the segment size and
// unclean leader election READ-ONLY, so the canonical profile arrives here as
// four settings and the size chain. Anything else in the profile is either
// MSK-managed or refused, and is reported rather than written.
resource "aws_msk_configuration" "this" {
  name           = var.name
  kafka_versions = [var.kafka_version]

  description = "DFE broker profile for ${var.name} (${var.env}) -- the Express-applicable subset"

  // auto.create.topics.enable stays FALSE: dfe-loader treats a missing *_land
  // topic as fatal, so the landing topics are pre-created by the bootstrap Job
  // rather than conjured by the first produce, which is what makes their
  // partition count and retention ours to set.
  server_properties = <<-PROPERTIES
    num.partitions=${var.num_partitions}
    log.retention.ms=${var.log_retention_ms}
    auto.create.topics.enable=false
    message.max.bytes=${var.message_max_bytes}
    replica.fetch.max.bytes=${var.message_max_bytes}
  PROPERTIES
}

// ---------------------------------------------------------------------------
// Broker logs -- otel (the default) or cloudwatch, never both.
// ---------------------------------------------------------------------------

// cloudwatch path only. Not encrypted with the deployment key: CloudWatch Logs
// writes as a service principal, and the cluster module's key policy delegates
// to IAM, which a service principal cannot use. Log data is encrypted at rest
// either way.
resource "aws_cloudwatch_log_group" "brokers" {
  count = var.telemetry.sink == "cloudwatch" ? 1 : 0

  name              = "/aws/msk/${var.name}"
  retention_in_days = var.telemetry.retention_days
}

// otel path only. Log spill, not data DFE keeps -- the fetcher's object-store
// source reads it into the OTel feed and the lifecycle rule below is the only
// thing that bounds it after that.
resource "aws_s3_bucket" "broker_logs" {
  count = var.telemetry.sink == "otel" ? 1 : 0

  bucket        = "${var.name}-msk-broker-logs"
  force_destroy = true

  tags = { Name = "${var.name}-msk-broker-logs" }
}

resource "aws_s3_bucket_public_access_block" "broker_logs" {
  count = var.telemetry.sink == "otel" ? 1 : 0

  bucket = aws_s3_bucket.broker_logs[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

// SSE-KMS on the deployment's own key -- unlike the cloudwatch path, S3 write
// access can be granted, because the log delivery service authenticates
// against a resource policy this module can extend rather than an IAM policy
// it cannot write into a service principal.
resource "aws_s3_bucket_server_side_encryption_configuration" "broker_logs" {
  count = var.telemetry.sink == "otel" ? 1 : 0

  bucket = aws_s3_bucket.broker_logs[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }

    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "broker_logs" {
  count = var.telemetry.sink == "otel" ? 1 : 0

  bucket = aws_s3_bucket.broker_logs[0].id

  rule {
    id     = "expire-broker-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = var.telemetry.retention_days
    }
  }
}

// aws_kms_key_policy REPLACES the key's whole policy, so this restates AWS's
// own default (the account root decides, via IAM -- see the cluster module's
// kms.tf) and adds the one statement AWS's own docs require before an
// SSE-KMS bucket can accept MSK's log delivery: the log delivery service
// principal is not an IAM identity, so it cannot be granted access to a
// customer key through an aws_iam_role_policy the way the cluster's own EKS
// role is. Applying this from here rather than from the cluster module is
// what lets a body that needs no S3 delivery -- confluent-cloud, redpanda-cloud
// -- leave the key's policy exactly as the cluster module set it.
resource "aws_kms_key_policy" "broker_logs" {
  count = var.telemetry.sink == "otel" ? 1 : 0

  key_id = var.kms_key_arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "Enable IAM User Permissions"
        Effect    = "Allow"
        Principal = { AWS = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid    = "Allow Amazon MSK to use the key"
        Effect = "Allow"
        Principal = {
          Service = ["delivery.logs.amazonaws.com"]
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ]
        Resource = "*"
      },
    ]
  })
}

// ---------------------------------------------------------------------------
// The cluster
// ---------------------------------------------------------------------------

resource "aws_msk_cluster" "this" {
  cluster_name           = var.name
  kafka_version          = var.kafka_version
  number_of_broker_nodes = var.broker_count

  broker_node_group_info {
    instance_type = local.broker_instance_type

    // PRIVATE subnets only, one per zone. Brokers are data plane; nothing
    // outside the VPC reaches them.
    client_subnets  = var.network.private_subnet_ids
    security_groups = [aws_security_group.brokers.id]

    // No storage_info: the provider refuses it on an Express instance type,
    // because Express storage is MSK-managed and grows on its own.
  }

  client_authentication {
    sasl {
      // SCRAM is what DFE's apps use. IAM is the bootstrap Job's only, because
      // IAM policy is the authorisation there and no Kafka ACL has to exist
      // yet -- which is how the first ACL gets created at all.
      scram = true
      iam   = true
    }
  }

  encryption_info {
    encryption_at_rest_kms_key_arn = var.kms_key_arn

    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.this.arn
    revision = aws_msk_configuration.this.latest_revision
  }

  // Partitions move onto a broker added later, which is what makes a 3 -> 6
  // scale-out spread the existing topics rather than only the new ones.
  rebalancing {
    status = var.rebalancing_status
  }

  logging_info {
    broker_logs {
      dynamic "cloudwatch_logs" {
        for_each = var.telemetry.sink == "cloudwatch" ? [1] : []

        content {
          enabled   = true
          log_group = aws_cloudwatch_log_group.brokers[0].name
        }
      }

      dynamic "s3" {
        for_each = var.telemetry.sink == "otel" ? [1] : []

        content {
          enabled = true
          bucket  = aws_s3_bucket.broker_logs[0].id
        }
      }
    }
  }

  open_monitoring {
    prometheus {
      jmx_exporter {
        enabled_in_broker = true
      }

      node_exporter {
        enabled_in_broker = true
      }
    }
  }

  // The secret CONTAINER exists before the cluster; its VERSION is written
  // after. That ordering is what keeps the SCRAM association off the cluster's
  // own creation path. On the otel path, the key policy grant has to exist
  // before MSK's first log delivery attempt too, or the write is refused --
  // depends_on takes the whole (possibly zero-count) resource, never an index.
  depends_on = [
    aws_secretsmanager_secret.scram,
    aws_kms_key_policy.broker_logs,
  ]

  lifecycle {
    // MSK rewrites both of these out of band -- the configuration when a
    // revision is applied through the API, the SASL block when authentication
    // is changed on a live cluster -- and tofu would fight it on every plan.
    // A deliberate change to either is applied by removing this ignore for the
    // one apply that lands it. Express carries no ebs_storage_info, so
    // dfe-core's third ignore has nothing to name here.
    ignore_changes = [
      configuration_info,
      client_authentication,
    ]
  }
}
