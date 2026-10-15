output "node_user_data" {
  description = "The shell script every node runs before kubelet starts, pointing containerd at the cache with each upstream as its fallback. Empty when nothing is cached."
  value       = local.node_user_data

  // A node launched from this script pulls through rules, templates and grants that already exist, and goes before the teardown that empties the cache.
  depends_on = [
    aws_ecr_pull_through_cache_rule.this,
    aws_ecr_repository_creation_template.this,
    aws_iam_role_policy.node_import,
    terraform_data.repository_teardown,
  ]
}

output "registry" {
  description = "The ECR registry host the cache is served from."
  value       = local.registry
}

output "mirrors" {
  description = "Each cached registry host and the ECR path containerd reads it from."
  value       = { for mirror in local.mirrors : mirror.host => mirror.endpoint }
}

output "uncached" {
  description = "Upstream keys ECR would cache only with a credential, which none was given for. Their images are pulled from the upstream directly."
  value       = sort([for prefix in keys(var.upstreams) : prefix if !contains(keys(local.active), prefix)])
}
