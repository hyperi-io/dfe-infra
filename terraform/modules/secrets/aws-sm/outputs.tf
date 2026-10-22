output "store_config" {
  description = "Everything the ESO ClusterSecretStore's aws provider block reads, and nothing the caller has to interpret."
  value = {
    provider = "aws"
    // The Secrets Manager half of ESO's aws provider, as opposed to
    // ParameterStore. The spelling is ESO's own.
    service = "SecretsManager"
    region  = data.aws_region.current.region
    // Pod Identity means no key, no AppRole and no secretRef in the store.
    auth = "pod-identity"
    // The ref alone: every remoteRef key already starts with <project>/<env>,
    // and ESO prepends this to it with no separator of its own.
    prefix = var.prefix
  }
}

output "eso_role_arn" {
  description = "The role ESO assumes. Read-only, fenced to this deployment's path less engine/, plus decrypt on its key."
  value       = aws_iam_role.eso.arn
}

output "engine_role_arn" {
  description = "The role dfe-engine's service account assumes. Writes under engine_name_prefix and nowhere else, with the key used only through Secrets Manager."
  value       = aws_iam_role.engine.arn
}

output "receiver_role_arn" {
  description = "The role dfe-receiver's service account assumes. Reads the ingest bearer-token secrets under engine_name_prefix and nothing else, with the key used only through Secrets Manager."
  value       = aws_iam_role.receiver.arn
}

output "engine_name_prefix" {
  description = "<path>/engine: every secret dfe-engine mints is named under it, and no seed is."
  value       = local.engine_name_prefix
}
