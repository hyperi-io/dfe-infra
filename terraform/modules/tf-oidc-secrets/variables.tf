variable "project" { type = string }
variable "env" { type = string }
variable "vault_mount" { type = string }
variable "namespace" {
  description = "K8s namespace for ExternalSecret resources"
  type        = string
}
variable "secret_store_name" {
  description = "ClusterSecretStore name for ESO"
  type        = string
  default     = "dfe-secret-store"
}
variable "providers" {
  description = "Map of OIDC provider configs"
  type = map(object({
    secret_keys = list(string)
  }))
  default = {}
}
