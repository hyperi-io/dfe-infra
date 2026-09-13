output "instance_id" {
  description = "Empty when the toolbox is not enabled -- dfe-ops bastion status/down reads this to tell 'never brought up' from 'torn down'."
  value       = try(aws_instance.this[0].id, "")
}

output "ssm_session_document" {
  description = "The SHELL Session document's name: `aws ssm start-session --target <instance_id> --document-name <this>`. Empty when not enabled."
  value       = try(aws_ssm_document.shell[0].name, "")
}

output "targets" {
  description = "Every named forward target, plus the per-target Session document `dfe-ops bastion forward <name>` invokes -- host and port are informational here; the document is what actually fixes them."
  value = {
    for name, target in var.targets : name => {
      host          = target.host
      port          = target.port
      document_name = try(aws_ssm_document.forward[name].name, "")
    }
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
