// Enterprise's private handshake: AWS PrivateLink, with a private hosted zone for the cluster domain.
// The security group below also serves Freight's interfaces. docs/deployment/kafka/confluent-cloud.md#module-design-notes.

locals {
  privatelink = local.private && var.tier == "enterprise"

  private_client_cidrs = length(var.client_cidrs) > 0 ? var.client_cidrs : [var.network.cidr]

  // 9092 is the Kafka protocol, 443 the REST endpoint and schema registry. Port 80 only redirects to 443.
  private_ports = [443, 9092]

  private_ingress_rules = local.private ? {
    for pair in setproduct(local.private_ports, local.private_client_cidrs) :
    "${pair[0]}-${pair[1]}" => { port = pair[0], cidr = pair[1] }
  } : {}
}

resource "aws_security_group" "private" {
  count = local.private ? 1 : 0

  name        = "${var.name}-confluent-private"
  description = "Confluent Cloud private attachment for ${var.name} (${var.env})"
  vpc_id      = var.network.vpc_id

  tags = { Name = "${var.name}-confluent-private" }
}

resource "aws_vpc_security_group_ingress_rule" "private" {
  for_each = local.private_ingress_rules

  security_group_id = aws_security_group.private[0].id

  cidr_ipv4   = each.value.cidr
  ip_protocol = "tcp"
  from_port   = each.value.port
  to_port     = each.value.port

  description = "Confluent Cloud clients on ${each.value.port}"
}

// No egress rule, deliberately: nothing in Confluent Cloud may open a connection into this network.

resource "confluent_private_link_attachment" "this" {
  count = local.privatelink ? 1 : 0

  display_name = "${var.name}-platt"
  cloud        = local.cloud
  region       = local.region

  environment {
    id = local.environment_id
  }
}

resource "aws_vpc_endpoint" "this" {
  count = local.privatelink ? 1 : 0

  vpc_id            = var.network.vpc_id
  service_name      = confluent_private_link_attachment.this[0].aws[0].vpc_endpoint_service_name
  vpc_endpoint_type = "Interface"

  subnet_ids         = var.network.private_subnet_ids
  security_group_ids = [aws_security_group.private[0].id]

  // The endpoint service has no verified private DNS name, so the hosted zone below resolves the cluster.
  private_dns_enabled = false

  tags = { Name = "${var.name}-confluent-privatelink" }
}

resource "confluent_private_link_attachment_connection" "this" {
  count = local.privatelink ? 1 : 0

  display_name = "${var.name}-plattc"

  environment {
    id = local.environment_id
  }

  aws {
    vpc_endpoint_id = aws_vpc_endpoint.this[0].id
  }

  private_link_attachment {
    id = confluent_private_link_attachment.this[0].id
  }
}

resource "aws_route53_zone" "privatelink" {
  count = local.privatelink ? 1 : 0

  name = confluent_private_link_attachment.this[0].dns_domain

  vpc {
    vpc_id = var.network.vpc_id
  }

  tags = { Name = "${var.name}-confluent-privatelink" }
}

locals {
  // The endpoint's regional DNS name, and its leading label that the zonal names are built from.
  endpoint_dns_name = local.privatelink ? aws_vpc_endpoint.this[0].dns_entry[0]["dns_name"] : ""
  endpoint_prefix   = local.privatelink ? split(".", local.endpoint_dns_name)[0] : ""
}

resource "aws_route53_record" "privatelink" {
  count = local.privatelink ? 1 : 0

  zone_id = aws_route53_zone.privatelink[0].zone_id
  name    = "*.${aws_route53_zone.privatelink[0].name}"
  type    = "CNAME"
  ttl     = 60

  records = [local.endpoint_dns_name]
}

// Brokers advertise zonal hostnames too, and a record per zone keeps a client's traffic in its own zone.
resource "aws_route53_record" "privatelink_zonal" {
  for_each = local.privatelink ? toset(var.network.azs) : toset([])

  zone_id = aws_route53_zone.privatelink[0].zone_id
  name    = "*.${data.aws_availability_zone.this[each.value].zone_id}"
  type    = "CNAME"
  ttl     = 60

  records = [
    format("%s-%s%s",
      local.endpoint_prefix,
      each.value,
      replace(local.endpoint_dns_name, local.endpoint_prefix, "")
    )
  ]
}
