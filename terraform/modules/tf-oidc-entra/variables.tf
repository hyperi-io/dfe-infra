variable "display_name" {
  description = "App registration display name"
  type        = string
  default     = "DFE Platform"
}
variable "domain" {
  description = "DFE domain for redirect URI"
  type        = string
}
variable "tenant_id" {
  description = "Entra tenant ID"
  type        = string
}
