output "store_config" {
  description = "Everything the ESO ClusterSecretStore's aws provider block reads, and nothing the caller has to interpret."
  value = {
    provider = "aws"
    // The Secrets Manager half of ESO's aws provider, as opposed to
    // ParameterStore. The spelling is ESO's own.
    service = "SecretsManager"
    region  = data.aws_region.current.region
    // Pod Identity means no key, no AppRole and no secretRef in the store.
    auth   = "pod-identity"
    prefix = local.path
  }
}

output "eso_role_arn" {
  description = "The role ESO assumes. Read-only, and fenced to this deployment's path plus decrypt on its key."
  value       = aws_iam_role.eso.arn
}
