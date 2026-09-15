output "instance_id" {
  description = "Empty when the toolbox is not enabled -- dfe-ops bastion status/down reads this to tell 'never brought up' from 'torn down'."
  value       = try(aws_instance.this[0].id, "")
}

output "ssm_session_document" {
  description = "The SHELL Session document's name: `aws ssm start-session --target <instance_id> --document-name <this>`. Empty when not enabled."
  value       = try(aws_ssm_document.shell[0].name, "")
}

output "targets" {
  description = "Every forward target that HAS both its Session document and an open egress rule, derived from those resources rather than from var.targets -- a target the module did not finish building is absent here instead of advertised, because `dfe-ops bastion status` reads this as the list of what works. A target on the control-plane port needs no rule of its own, that egress being open already. host and port are informational; document_name is what fixes them."
  value = {
    for name, doc in aws_ssm_document.forward : name => {
      host          = var.targets[name].host
      port          = var.targets[name].port
      document_name = doc.name
    }
    if var.targets[name].port == local.control_egress_port || contains(
      keys(aws_vpc_security_group_egress_rule.targets),
      "${var.targets[name].scope}-${var.targets[name].port}"
    )
  }
}

output "session_log_bucket" {
  description = "The S3 bucket every shell session's transcript lands in. Persists across up/down cycles -- see variables.tf's header comment."
  value       = aws_s3_bucket.session_logs.bucket
}

output "security_group_id" {
  description = "Empty when the toolbox is not enabled."
  value       = try(aws_security_group.this[0].id, "")
}

output "iam_role_name" {
  description = "Empty when the toolbox is not enabled. Named for a `down` proof that checks no stray role survives, and for CONTRACT.md's policy examples."
  value       = try(aws_iam_role.this[0].name, "")
}
