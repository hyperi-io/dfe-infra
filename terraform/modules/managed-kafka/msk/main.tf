// MSK Provisioned on Express brokers, SCRAM for the apps and IAM for the bootstrap Job.
// Why: docs/deployment/kafka/aws-msk-operations.md#module-design-notes.

locals {
  // MSK takes one instance type, so the resolver's best-first list collapses to its head.
  broker_instance_type = var.resolved_shapes[var.broker_shape_ref].instance_types[0]

  client_cidrs = length(var.client_cidrs) > 0 ? var.client_cidrs : [var.network.cidr]

  // Fixed by MSK: 9096 is SASL/SCRAM over TLS, 9098 SASL/IAM.
  broker_ports = {
    scram = 9096
    iam   = 9098
  }

  ingress_rules = {
    for pair in setproduct(keys(local.broker_ports), local.client_cidrs) :
    "${pair[0]}-${pair[1]}" => { port = local.broker_ports[pair[0]], cidr = pair[1] }
  }

  // Fixed by open_monitoring: JMX 11001, node exporter 11002. Kept apart from broker_ports so they stay VPC-wide.
  open_monitoring_ports = {
    jmx  = 11001
    node = 11002
  }
}

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

// The scrape runs in-cluster, so it is VPC-wide and a narrowed client_cidrs cannot cut it off.
resource "aws_vpc_security_group_ingress_rule" "open_monitoring" {
  for_each = local.open_monitoring_ports

  security_group_id = aws_security_group.brokers.id

  cidr_ipv4   = var.network.cidr
  ip_protocol = "tcp"
  from_port   = each.value
  to_port     = each.value

  description = "Prometheus scrape on ${each.value}"
}

// Egress 443 to anywhere: Secrets Manager, KMS and CloudWatch Logs have no VPC endpoint here,
// and AWS publishes a managed prefix list for only the first.
resource "aws_vpc_security_group_egress_rule" "brokers" {
  security_group_id = aws_security_group.brokers.id

  cidr_ipv4   = "0.0.0.0/0"
  ip_protocol = "tcp"
  from_port   = 443
  to_port     = 443

  description = "Broker egress to the AWS services MSK depends on (Secrets Manager, KMS, CloudWatch Logs), none of which sit behind a VPC interface endpoint here"
}

// Express fixes the thread pools, replication factor, segment size and unclean leader election, so only these apply.
resource "aws_msk_configuration" "this" {
  name           = var.name
  kafka_versions = [var.kafka_version]

  description = "DFE broker profile for ${var.name} (${var.env}) -- the Express-applicable subset"

  // auto.create.topics.enable stays false: dfe-engine creates the landing topics, and dfe-loader treats a missing one as fatal.
  server_properties = <<-PROPERTIES
    num.partitions=${var.num_partitions}
    log.retention.ms=${var.log_retention_ms}
    auto.create.topics.enable=false
    message.max.bytes=${var.message_max_bytes}
    replica.fetch.max.bytes=${var.message_max_bytes}
  PROPERTIES
}

// cloudwatch sink only. Not KMS-encrypted: a service principal cannot use a key policy that delegates to IAM.
resource "aws_cloudwatch_log_group" "brokers" {
  count = var.telemetry.sink == "cloudwatch" ? 1 : 0

  name              = "/aws/msk/${var.name}"
  retention_in_days = var.telemetry.retention_days
}

// otel sink only. The fetcher's object_store source reads it and the lifecycle rule bounds it.
resource "aws_s3_bucket" "broker_logs" {
  count = var.telemetry.sink == "otel" ? 1 : 0

  bucket        = "${var.s3_bucket_prefix}${var.name}-msk-broker-logs"
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

// SSE-KMS on the deployment key. The key policy grant for log delivery comes from the cluster module's key_policy_grants.
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

resource "aws_msk_cluster" "this" {
  cluster_name           = var.name
  kafka_version          = var.kafka_version
  number_of_broker_nodes = var.broker_count

  broker_node_group_info {
    instance_type = local.broker_instance_type

    // Private subnets only, one per zone.
    client_subnets  = var.network.private_subnet_ids
    security_groups = [aws_security_group.brokers.id]

    // No storage_info: Express storage is MSK-managed and the provider refuses the block.
  }

  client_authentication {
    sasl {
      // SCRAM for the apps, IAM for the bootstrap Job alone.
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

  // Moves partitions onto brokers added later.
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

  // The secret container exists before the cluster and its version after, keeping the SCRAM association off the creation path.
  depends_on = [
    aws_secretsmanager_secret.scram,
  ]

  lifecycle {
    // MSK rewrites both out of band, so tofu would fight it every plan. Drop the ignore for the one apply that lands a deliberate change.
    ignore_changes = [
      configuration_info,
      client_authentication,
    ]
  }
}
