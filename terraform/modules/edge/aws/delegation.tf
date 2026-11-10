// The parent zone's NS record for the public zone, when the parent is in this account.
locals {
  // How long resolvers may keep a destroyed run's nameservers.
  delegation_ttl_seconds = 300
}

resource "aws_route53_record" "delegation" {
  count = var.dns.public_zone != "" && var.dns.parent_zone_id != "" ? 1 : 0

  zone_id = var.dns.parent_zone_id
  name    = var.dns.public_zone
  type    = "NS"
  ttl     = local.delegation_ttl_seconds
  records = aws_route53_zone.public[0].name_servers
}
