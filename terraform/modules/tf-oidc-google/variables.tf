variable "project_id" {
  description = "Google Cloud project ID"
  type        = string
}
variable "domain" {
  description = "DFE domain for redirect URI"
  type        = string
}
variable "google_workspace_domain" {
  description = "Google Workspace domain for Admin SDK delegation"
  type        = string
  default     = ""
}
