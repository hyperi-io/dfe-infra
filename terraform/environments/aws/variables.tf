// The same inputs the on-prem path takes -- account, domain, CIDR -- plus the
// region, which on-prem does not have.

variable "region" {
  description = "AWS region. The dfe-test account is held to us-west-2 by SCP DenyOutsideUSWest2."
  type        = string
  default     = "us-west-2"
}

variable "name" {
  description = "Name prefix for every resource."
  type        = string
  default     = "dfe-spike"
}

variable "vpc_cidr" {
  description = "VPC CIDR. Must not overlap the account's default VPC."
  type        = string
  default     = "10.90.0.0/16"

  validation {
    condition     = !startswith(var.vpc_cidr, "172.31.")
    error_message = "vpc_cidr collides with the account's default VPC (172.31.0.0/16)."
  }
}

variable "domain" {
  description = <<-EOT
    Private Route 53 zone external-dns writes into. Private so the path is
    exercised without delegating anything from the Cloudflare-hosted hyperi.io.
  EOT
  type        = string
  default     = "dfe-spike.internal"
}

variable "kubernetes_version" {
  description = "EKS version. 1.36 is the EKS default and matches the platform floor in versions.yaml."
  type        = string
  default     = "1.36"
}

variable "node_instance_types" {
  description = <<-EOT
    Node sizes. A DFE scale deploy requests roughly 30 GiB across ClickHouse,
    Keeper, Kafka, CNPG and the app replicas, so three 16 GiB nodes carry one
    deploy with room for the daemonsets. Several types are offered so a single
    spot pool running dry does not strand the node group.
  EOT
  type        = list(string)
  default     = ["m5.xlarge", "m5a.xlarge", "m6i.xlarge"]
}

variable "node_capacity_type" {
  description = <<-EOT
    SPOT. This is a throwaway cluster, an interruption costs a rescheduled pod,
    and spot is roughly 70% cheaper -- which is most of what keeps the exercise
    inside its budget.
  EOT
  type        = string
  default     = "SPOT"
}

variable "node_desired_size" {
  description = "Node count. Three, so ClickHouse, Keeper and Kafka can spread one replica each."
  type        = number
  default     = 3
}
