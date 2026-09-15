output "instance_id" {
  description = "Empty when the toolbox is not enabled -- dfe-ops bastion status/down reads this to tell 'never brought up' from 'torn down'."
  value       = try(aws_instance.this[0].id, "")
}

output "ssm_session_document" {
  description = "The SHELL Session document's name: `aws ssm start-session --target <instance_id> --document-name <this>`. Empty when not enabled."
  value       = try(aws_ssm_document.shell[0].name, "")
}

output "targets" {
  description = "Every forward target that HAS its Session document, derived from that resource rather than from var.targets -- a target the module did not finish building is absent here instead of advertised, because `dfe-ops bastion status` reads this as the list of what works. host and port are informational; document_name is what fixes them. Every non-443 target gets an egress rule by construction (local.target_egress filters var.targets on the port alone), and 443 is open already, so there is no port to filter on here."
  value = {
    for name, doc in aws_ssm_document.forward : name => {
      host          = var.targets[name].host
      port          = var.targets[name].port
      document_name = doc.name
    }
  }
}

output "session_log_bucket" {
  description = "The S3 bucket every shell session's transcript lands in. Persists across up/down cycles -- see variables.tf's header comment."
  value       = aws_s3_bucket.session_logs.bucket
}

output "admin_cidr" {
  description = "The instance's own private address as a /32, which is the range culvert's admin exception is asked to admit. A /32 rather than the subnet because the subnet is a /20 the EKS node groups, Karpenter and every pod under the VPC CNI also hold addresses in. Empty when the toolbox is not enabled, so the hole closes with the instance."
  value       = try("${aws_instance.this[0].private_ip}/32", "")
}

output "security_group_id" {
  description = "Empty when the toolbox is not enabled."
  value       = try(aws_security_group.this[0].id, "")
}

output "iam_role_name" {
  description = "Empty when the toolbox is not enabled. Named for a `down` proof that checks no stray role survives, and for CONTRACT.md's policy examples."
  value       = try(aws_iam_role.this[0].name, "")
}
