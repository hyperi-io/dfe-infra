// Each validation below is a refusal at plan. The messages name the dial field
// a deployer sets, since that is the one they can change.

variable "cloud" {
  description = "The cloud the deployment runs on, the root's provision.cloud: aws, gcp or azure."
  type        = string

  validation {
    condition     = contains(keys(local.offering.clouds), var.cloud)
    error_message = "cloud is ${var.cloud}, and offering.yaml covers ${join(", ", sort(keys(local.offering.clouds)))}. A managed backend is offered by the cloud the deployment runs on, so a cloud with no list offers none."
  }
}

variable "kafka_provider" {
  description = "The dial's kafka.provider. strimzi and redpanda run in the cluster and are allowed on every cloud; a managed provider must be in this cloud's list in offering.yaml."
  type        = string

  validation {
    condition     = contains(local.in_cluster_kafka, var.kafka_provider) || contains(keys(local.offered_kafka), var.kafka_provider)
    error_message = "kafka.provider is ${var.kafka_provider}, which is not offered on ${var.cloud}. A managed backend is offered only as its instance on the deployment's own cloud, because one on another cloud is reached over the internet and every byte to it is billed as internet egress. On ${var.cloud} the managed Kafka offered is ${length(local.offered_kafka) > 0 ? join(", ", sort(keys(local.offered_kafka))) : "none"}, and a Kafka in the cluster (strimzi, redpanda) runs on any cloud."
  }
}

variable "kafka_tier" {
  description = "The managed Kafka's tier, where its body takes one: the dial's kafka.confluent_tier on confluent-cloud. Null where the body has no tier to choose."
  type        = string
  default     = null

  validation {
    condition     = var.kafka_tier == null || !local.kafka_offered || length(local.kafka_tiers) == 0 || contains(local.kafka_tiers, coalesce(var.kafka_tier, "-"))
    error_message = "The ${var.kafka_provider} tier ${coalesce(var.kafka_tier, "-")} is not offered on ${var.cloud}: the vendor does not run it there. On ${var.cloud} the ${var.kafka_provider} tiers offered are ${join(", ", local.kafka_tiers)}."
  }
}

variable "clickhouse_mode" {
  description = "The dial's clickhouse.mode: in_cluster or external."
  type        = string
  default     = "in_cluster"

  validation {
    condition     = contains(["in_cluster", "external"], var.clickhouse_mode)
    error_message = "clickhouse_mode must be in_cluster or external."
  }
}

// The cloud a ClickHouse Cloud host names is checked by output.clickhouse's
// precondition: a validation here could not read the label derived from this
// same variable.
variable "clickhouse_host" {
  description = "The ClickHouse host the deployment connects to (the dial's clickhouse.host, the root's endpoints.clickhouse_host). On external mode a host under a ClickHouse Cloud domain is that vendor's service, and its cloud label must be the deployment's own cloud. Any other host is a server the deployer runs, which no cloud has to offer."
  type        = string
  default     = ""
}
