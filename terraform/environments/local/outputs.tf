output "DFE_ENV" {
  value = local.env
}

output "DFE_CLOUD" {
  value = local.cloud
}

output "DFE_REGION" {
  value = local.region
}

output "DFE_DOMAIN" {
  value = var.domain
}

output "DFE_PROFILE" {
  value = var.profile
}

output "DFE_REPO_URL" {
  value = var.repo_url
}

output "DFE_TARGET_REVISION" {
  value = var.target_revision
}

output "DFE_STORAGE_CLASS" {
  value = "local-path"
}

output "DFE_NAMESPACE" {
  value = module.naming.k8s_namespace
}

output "DFE_CLICKHOUSE_HOST" {
  # The clickhouse-operator names the service dfe-clickhouse (release dfe), NOT
  # clickhouse -- the old value did not resolve, so the engine's CH client failed
  # NameResolutionError. Matches argocd/values/common.yaml + hyperdx/otel values.
  value = "dfe-clickhouse.clickhouse.svc.cluster.local"
}

output "DFE_KAFKA_BOOTSTRAP" {
  value = "dfe-kafka-kafka-bootstrap.strimzi.svc.cluster.local:9092"
}

output "DFE_OTEL_ENDPOINT" {
  value = "otel-collector-gateway.otel.svc.cluster.local:4317"
}

output "DFE_VAULT_ADDR" {
  value = module.secrets.vault_addr
}

output "DFE_VAULT_ROLE_ID" {
  value     = module.secrets.eso_role_id
  sensitive = true
}

output "DFE_VAULT_SECRET_ID" {
  # Surfaced so bootstrap can seed the ESO AppRole secret (dfe-vault-approle-secret).
  # Nothing else created that k8s secret, so ESO could never authenticate to OpenBao.
  value     = module.secrets.eso_secret_id
  sensitive = true
}

output "DFE_WORKLOAD_IDENTITY_ANNOTATIONS" {
  value = jsonencode(module.iam.workload_identity_annotations)
}

output "DFE_REGISTRY_HOST" {
  value = var.registry_host
}

output "DFE_REGISTRY_USER" {
  value = var.registry_user
}

output "DFE_REGISTRY_TOKEN" {
  value     = var.registry_token
  sensitive = true
}
