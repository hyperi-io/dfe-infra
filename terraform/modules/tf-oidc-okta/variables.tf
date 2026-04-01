variable "okta_domain" {
  description = "Okta org domain (e.g. dev-12345.okta.com)"
  type        = string
}
variable "domain" {
  description = "DFE domain for redirect URI"
  type        = string
}
variable "create_api_token" {
  description = "Create Okta API token for Groups API (optional)"
  type        = bool
  default     = false
}
