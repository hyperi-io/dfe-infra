output "public_zone_id" {
  description = "Route 53 zone the public UI names resolve in. Empty when no public zone was asked for."
  value       = try(aws_route53_zone.public[0].zone_id, "")
}

output "public_zone_name_servers" {
  description = "The NS set the PARENT zone has to delegate to before any public name resolves. Empty when there is no public zone."
  value       = try(aws_route53_zone.public[0].name_servers, [])
}

output "external_dns_role_arn" {
  description = "The role external-dns assumes to write both zones. Already associated with its service account, so this is for the record rather than for wiring."
  value       = aws_iam_role.external_dns.arn
}

output "cert_manager_role_arn" {
  description = "The role cert-manager assumes for a DNS-01 challenge on the public zone. Empty when there is no public zone, because the challenge has nowhere to write."
  value       = try(aws_iam_role.cert_manager[0].arn, "")
}

output "tunnel_address" {
  description = "The Elastic IP clients dial, and what external-dns publishes vpn.serverCN at. Empty on address.mode byo, where the deployer brings an address this module never sees."
  value       = try(aws_eip.forwarder[0].public_ip, "")
}

output "tunnel_listener_ports" {
  description = "The UDP ports clients dial, following the culvert chart's own listeners list. What the toolbox opens egress to when it joins the hub as an admin peer, so the ports are named once here rather than restated by every caller. Empty on address.mode byo, travelling with tunnel_address: a port with no address to aim it at renders an egress rule to `/32`."
  value       = local.forwarder_enabled ? [for p in local.tunnel_ports : p.port] : []
}

output "tunnel_zone" {
  description = "The availability zone the forwarder is pinned to, so the deployment can give culvert a matching nodeSelector -- a hop to a node in another zone crosses a boundary billed per GB. Empty on address.mode byo."
  value       = local.forwarder_enabled ? local.forwarder_zone : ""
}

output "load_balancer_controller_role_arn" {
  description = "The role the AWS Load Balancer Controller assumes. Already associated with its service account; named here so a caller can prove which identity reconciles the deployment's load balancers."
  value       = aws_iam_role.lbc.arn
}
