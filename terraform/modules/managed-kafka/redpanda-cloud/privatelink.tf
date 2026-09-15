// The private handshake: the AWS end. Redpanda creates the VPC endpoint service
// and we create the interface endpoint that connects to it, in the same private
// subnets the DFE pods run on.
//
// private_dns_enabled is what makes the cluster's own hostnames resolve inside
// this VPC, which is why no hosted zone is created here. Another VPC or an
// on-prem network resolves the cluster domain through a Route 53 Resolver
// inbound endpoint plus a forwarding rule -- never by pointing a rule at the
// VPC's own .2 resolver, which Redpanda documents as not working.

locals {
  // An empty client_cidrs means the whole VPC -- the private network the DFE
  // pods already sit on.
  endpoint_client_cidrs = length(var.client_cidrs) > 0 ? var.client_cidrs : [var.network.cidr]

  // The ports Redpanda Cloud serves behind the endpoint: 9092 is the Kafka
  // bootstrap, 9093 to 9095 the brokers, 8081 the schema registry and 443 the
  // data plane API and Prometheus. Fixed by the vendor.
  endpoint_ports = [443, 8081, 9092, 9093, 9094, 9095]

  // One ingress rule per port per allowed range.
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

  // The endpoint service publishes a verified private DNS name, so the seed
  // brokers resolve to this endpoint from inside the VPC with no zone of ours.
  private_dns_enabled = true

  tags = { Name = "${var.name}-redpanda-privatelink" }
}
