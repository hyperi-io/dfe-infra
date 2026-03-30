output "canonical_name" {
  description = "Primary resource name: {project}-{component}-{env}. Max 30 chars (GCP SA constraint)."
  value       = local.canonical_name
}

output "k8s_namespace" {
  description = "Kubernetes Namespace name: {project}-{env}."
  value       = local.k8s_namespace
}

output "k8s_service_account" {
  description = "Kubernetes ServiceAccount name: {project}-{component}."
  value       = local.k8s_service_account
}

output "rancher_cluster_name" {
  description = "Rancher cluster name: {project}-{cloud}-{env}."
  value       = local.rancher_cluster_name
}

output "rancher_project_name" {
  description = "Rancher Project name: {project}-{env}."
  value       = local.rancher_project_name
}

output "aws_irsa_role_name" {
  description = "AWS IAM Role name for IRSA: {canonical_name}-irsa."
  value       = local.aws_irsa_role_name
}

output "gcp_sa_account_id" {
  description = "GCP Service Account account_id (≤30 chars): equals canonical_name."
  value       = local.gcp_sa_account_id
}

output "azure_identity_name" {
  description = "Azure User-Assigned Managed Identity name: id-{canonical_name}."
  value       = local.azure_identity_name
}

output "azure_federated_cred_name" {
  description = "Azure Federated Identity Credential name: fc-{canonical_name}."
  value       = local.azure_federated_cred_name
}

output "vault_approle_name" {
  description = "OpenBao/Vault AppRole name: equals canonical_name."
  value       = local.vault_approle_name
}

output "vault_policy_name" {
  description = "OpenBao/Vault policy name: {canonical_name}-policy."
  value       = local.vault_policy_name
}

output "vault_secret_path" {
  description = "OpenBao/Vault KV path: secret/data/{project}/{env}/{component}."
  value       = local.vault_secret_path
}

output "common_tags" {
  description = "Map of cloud resource tags (lowercase underscore keys)."
  value       = local.common_tags
}
