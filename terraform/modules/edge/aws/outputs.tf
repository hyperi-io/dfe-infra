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

output "load_balancer_controller_role_arn" {
  description = "The role the AWS Load Balancer Controller assumes. Already associated with its service account; named here so a caller can prove which identity reconciles the deployment's load balancers."
  value       = aws_iam_role.lbc.arn
}
