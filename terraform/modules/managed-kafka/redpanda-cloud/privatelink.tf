// The AWS end of the PrivateLink handshake: an interface endpoint to Redpanda's endpoint service, in the DFE pods' subnets.
// private_dns_enabled resolves the cluster inside the VPC, so there is no hosted zone. Other networks: docs/deployment/kafka/redpanda-cloud.md#module-design-notes.

locals {
  endpoint_client_cidrs = length(var.client_cidrs) > 0 ? var.client_cidrs : [var.network.cidr]

  // Fixed by Redpanda: 9092 Kafka bootstrap, 9093 to 9095 brokers, 8081 schema registry, 443 data plane API and Prometheus.
  endpoint_ports = [443, 8081, 9092, 9093, 9094, 9095]

  endpoint_ingress_rules = local.private ? {
    for pair in setproduct(local.endpoint_ports, local.endpoint_client_cidrs) :
    "${pair[0]}-${pair[1]}" => { port = pair[0], cidr = pair[1] }
  } : {}
}

resource "aws_security_group" "endpoint" {
  count = local.private ? 1 : 0

  name        = "${var.name}-redpanda-privatelink"
  description = "Redpanda Cloud PrivateLink endpoint for ${var.name} (${var.env})"
  vpc_id      = var.network.vpc_id

  tags = { Name = "${var.name}-redpanda-privatelink" }
}

resource "aws_vpc_security_group_ingress_rule" "endpoint" {
  for_each = local.endpoint_ingress_rules

  security_group_id = aws_security_group.endpoint[0].id

  cidr_ipv4   = each.value.cidr
  ip_protocol = "tcp"
  from_port   = each.value.port
  to_port     = each.value.port

  description = "Redpanda Cloud clients on ${each.value.port}"
}

resource "aws_vpc_endpoint" "this" {
  count = local.private ? 1 : 0

  vpc_id            = var.network.vpc_id
  service_name      = redpanda_serverless_private_link.this[0].status.aws.vpc_endpoint_service_name
  vpc_endpoint_type = "Interface"

  subnet_ids         = var.network.private_subnet_ids
  security_group_ids = [aws_security_group.endpoint[0].id]

  // The service publishes a verified private DNS name, so the seed brokers resolve to this endpoint with no zone of ours.
  private_dns_enabled = true

  tags = { Name = "${var.name}-redpanda-privatelink" }
}
