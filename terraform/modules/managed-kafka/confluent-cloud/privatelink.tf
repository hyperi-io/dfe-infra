// Enterprise's private handshake: AWS PrivateLink.
//
// Confluent publishes a VPC endpoint SERVICE through a private link attachment,
// we create the interface endpoint that connects to it, and the attachment
// connection tells Confluent which endpoint arrived. Private DNS is ours to
// run here -- the endpoint service publishes no verified name, so the cluster's
// domain is served from a private hosted zone in this VPC.
//
// The security group below serves both private shapes: the PrivateLink endpoint
// on Enterprise and the Private Network Interfaces on Freight.

locals {
  privatelink = local.private && var.tier == "enterprise"

  // An empty client_cidrs means the whole VPC -- the private network the DFE
  // pods already sit on.
  private_client_cidrs = length(var.client_cidrs) > 0 ? var.client_cidrs : [var.network.cidr]

  // 9092 carries the Kafka protocol and 443 the REST endpoint and the schema
  // registry. Both are Confluent's. Port 80 is left out: it exists only to
  // redirect to 443 and nothing in DFE speaks it.
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

// No egress rule, deliberately. A security group tofu creates starts with none,
// which denies all outbound, and that is what Confluent asks for: nothing in
// Confluent Cloud should be able to open a connection INTO this network.

// ---------------------------------------------------------------------------
// Confluent's end of the attachment
// ---------------------------------------------------------------------------

resource "confluent_private_link_attachment" "this" {
  count = local.privatelink ? 1 : 0

  display_name = "${var.name}-platt"
  cloud        = local.cloud
  region       = local.region

  environment {
    id = confluent_environment.this.id
  }
}

resource "aws_vpc_endpoint" "this" {
  count = local.privatelink ? 1 : 0

  vpc_id            = var.network.vpc_id
  service_name      = confluent_private_link_attachment.this[0].aws[0].vpc_endpoint_service_name
  vpc_endpoint_type = "Interface"

  subnet_ids         = var.network.private_subnet_ids
  security_group_ids = [aws_security_group.private[0].id]

  // The endpoint service publishes no verified private DNS name, so AWS cannot
  // resolve the cluster for us and the hosted zone below does it instead.
  private_dns_enabled = false

  tags = { Name = "${var.name}-confluent-privatelink" }
}

resource "confluent_private_link_attachment_connection" "this" {
  count = local.privatelink ? 1 : 0

  display_name = "${var.name}-plattc"

  environment {
    id = confluent_environment.this.id
  }

  aws {
    vpc_endpoint_id = aws_vpc_endpoint.this[0].id
  }

  private_link_attachment {
    id = confluent_private_link_attachment.this[0].id
  }
}

// ---------------------------------------------------------------------------
// Resolving the cluster inside the VPC
// ---------------------------------------------------------------------------

resource "aws_route53_zone" "privatelink" {
  count = local.privatelink ? 1 : 0

  name = confluent_private_link_attachment.this[0].dns_domain

  vpc {
    vpc_id = var.network.vpc_id
  }

  tags = { Name = "${var.name}-confluent-privatelink" }
}

locals {
  // The endpoint's regional DNS name, and the leading label a zonal name is
  // built from -- vpce-0123-abcd.vpce-svc-0123.us-west-2.vpce.amazonaws.com
  // becomes vpce-0123-abcd plus the rest.
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

// The brokers advertise a zonal hostname as well as the wildcard one, so each
// zone gets a record pointing at that zone's own endpoint interface -- which is
// also what keeps a client's traffic inside its own availability zone.
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
