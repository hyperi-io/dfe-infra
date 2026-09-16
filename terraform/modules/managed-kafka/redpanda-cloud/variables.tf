// The contract, as OpenTofu sees it. Every input is described in CONTRACT.md;
// the validation blocks here are the half a reader cannot get from the types.
//
// Redpanda Cloud sizes and tunes the cluster itself, so several contract inputs
// arrive and are not applied. They are declared rather than dropped, because a
// body that refuses an input the contract names has changed the contract; what
// this implementation cannot pass on is named in the description and in
// CONTRACT.md.

variable "implementation" {
  description = "Which body of the managed-kafka contract the caller asked for. This one answers to redpanda-cloud and refuses anything else, so a mis-wired root fails at plan rather than building the wrong Kafka."
  type        = string
  default     = "redpanda-cloud"

  validation {
    condition     = var.implementation == "redpanda-cloud"
    error_message = "This body implements redpanda-cloud. An msk, confluent-cloud or strimzi deployment calls a different one."
  }
}

variable "connectivity" {
  description = "private puts the cluster behind an AWS PrivateLink endpoint in the deployment VPC and disables its public endpoint. public is the reachable-from-anywhere shape and has to be asked for."
  type        = string
  default     = "private"

  validation {
    condition     = contains(["private", "public"], var.connectivity)
    error_message = "connectivity must be private or public."
  }
}

variable "tier" {
  description = "Which Redpanda Cloud product carries the cluster. Serverless is the only one this organisation can create; Dedicated and BYOC are Request-access and priced by quote, so they are accepted here and refused at plan rather than silently building the wrong thing."
  type        = string
  default     = "serverless"

  validation {
    condition     = contains(["serverless", "dedicated", "byoc"], var.tier)
    error_message = "tier must be serverless, dedicated or byoc."
  }

  validation {
    // Dedicated and BYOC are behind Request access and have no public rate
    // card, so neither can be provisioned from a deployment config. The shape
    // is redpanda_network plus redpanda_cluster with cluster_type set to the
    // tier; it lands here once a quote and access exist.
    condition     = var.tier == "serverless"
    error_message = "Redpanda Dedicated and BYOC are quote-only: both are Request-access on this organisation and neither publishes a rate card, so this module cannot provision them. Use tier = serverless, or take the written quote to a Redpanda account team first."
  }
}

variable "name" {
  description = "Name prefix for every resource this module creates, and the cluster's own name."
  type        = string

  validation {
    // Redpanda's resource group takes letters, digits and hyphens, which is the
    // narrower of the two name rules this body has to satisfy.
    condition     = can(regex("^[a-zA-Z0-9-]{3,100}$", var.name))
    error_message = "name must be letters, digits and hyphens only, and at least 3 characters -- Redpanda's own floor for a cluster name."
  }
}

variable "env" {
  description = "Deployment environment, for the names and descriptions that have to distinguish two deployments in one organisation."
  type        = string
}

variable "network" {
  description = "The cluster module's network output. On private connectivity the PrivateLink endpoint lands in the PRIVATE subnets of this VPC; nothing else here touches the network."
  type = object({
    vpc_id             = string
    cidr               = string
    azs                = list(string)
    private_subnet_ids = list(string)
    public_subnet_ids  = list(string)
  })

  validation {
    condition     = length(var.network.private_subnet_ids) >= 2
    error_message = "The PrivateLink endpoint needs a subnet in at least two availability zones."
  }
}

variable "client_cidrs" {
  description = "Address ranges allowed to reach the PrivateLink endpoint. Empty means the whole VPC the cluster module built, which is the private network the DFE pods already sit on."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.client_cidrs, "0.0.0.0/0")
    error_message = "0.0.0.0/0 is not an allowlist. Name the ranges that may reach the endpoint, or leave this empty for the VPC."
  }
}

variable "scram_username" {
  description = "The one SCRAM principal the DFE fleet authenticates as, matching the single dfe-kafka-user the Strimzi and MSK paths already grant."
  type        = string
  default     = "dfe-kafka-user"
}

variable "scram_password" {
  description = "The SCRAM password, from the secrets module's kafka seed. Sensitive, and it reaches Redpanda as a write-only argument so it is never written to state."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.scram_password) >= 3
    error_message = "Redpanda rejects a password shorter than 3 characters."
  }
}

variable "scram_password_version" {
  description = "Bumped to push a new password through. A write-only argument cannot be compared between plan and apply, so this number is what tells Redpanda the value changed."
  type        = number
  default     = 1
}

variable "landing_topics" {
  description = "The topics created before DFE starts, keyed by topic name. dfe-loader treats a missing *_land topic as fatal, and Redpanda BYOC and Dedicated do not accept auto.create.topics.enable at all, so every path here pre-creates them. A null partition count or retention takes the tuning value below."
  type = map(object({
    partitions   = optional(number)
    retention_ms = optional(number)
  }))
  default = {}
}

variable "consumer_group_prefix" {
  description = "The consumer groups the SCRAM principal may read. The on-prem grant fences groups to this prefix rather than allowing all of them, and this body matches it."
  type        = string
  default     = "dfe-"
}

variable "allow_deletion" {
  description = "Whether tofu may destroy the cluster, the user and the topics. The provider defaults every one of these to false, which makes tofu destroy refuse; a provision-test-destroy proof needs true, and a customer deployment sets it false."
  type        = bool
  default     = true
}

variable "num_partitions" {
  description = "Default partitions for a landing topic that does not name its own. Derived by the chart from the same rule (S1.T7): a highly divisible multiple of the broker count, at least the consumer-parallelism ceiling. No default: the caller's dial states it, so a value cannot drift from msk's and confluent-cloud's copies of the same setting silently."
  type        = number

  validation {
    condition     = var.num_partitions > 0
    error_message = "num_partitions must be positive."
  }
}

variable "log_retention_ms" {
  description = "How long a landing topic keeps data by default, in milliseconds. The buffer has to survive the longest consumer outage plus the archiver's lag; 259200000 is three days. No default: the caller's dial states it, so a value cannot drift from msk's and confluent-cloud's copies of the same setting silently."
  type        = number

  validation {
    condition     = var.log_retention_ms > 0
    error_message = "log_retention_ms must be positive."
  }
}

variable "message_max_bytes" {
  description = "The largest record the topics accept, and the same number the scalo producer and consumer carry. Applied per topic as max.message.bytes, because Redpanda Cloud holds the broker-wide setting. No default: the caller's dial states it, so a value cannot drift from msk's and confluent-cloud's copies of the same setting silently."
  type        = number

  validation {
    condition     = var.message_max_bytes > 0
    error_message = "message_max_bytes must be positive."
  }
}

// ---------------------------------------------------------------------------
// Contract inputs Redpanda Cloud manages for us
// ---------------------------------------------------------------------------

variable "broker_shape_ref" {
  description = "NOT APPLIED. Redpanda Cloud sizes Serverless itself and exposes no instance type, so the resolver's shape has nothing to select here. Declared because the contract names it."
  type        = string
  default     = ""
}

variable "resolved_shapes" {
  description = "NOT APPLIED, for the same reason as broker_shape_ref. A Dedicated or BYOC cluster would map this onto throughput_tier, which is why the input stays in the contract."
  type = map(object({
    instance_types = list(string)
    arch           = string
  }))
  default = {}
}

variable "broker_count" {
  description = "NOT APPLIED. Serverless has no broker count to set -- the vendor scales it to the 100 MB/s write ceiling and bills realised usage."
  type        = number
  default     = 3
}

variable "kafka_version" {
  description = "NOT APPLIED. Redpanda Cloud exposes no Kafka version: it runs its own broker with Kafka-protocol compatibility, so the hard deck (S0.T3) binds on-prem Strimzi and MSK and not this body."
  type        = string
  default     = ""
}

variable "kms_key_arn" {
  description = "NOT APPLIED. Serverless encrypts at rest with Redpanda's own key and the provider exposes no customer-managed key argument."
  type        = string
  default     = ""
}

variable "eks_cluster_name" {
  description = "NOT APPLIED. There is no in-cluster bootstrap Job on this path -- the vendor provider writes the ACLs and topics directly, so no workload identity is minted."
  type        = string
  default     = ""
}

variable "pod_identity" {
  description = "NOT APPLIED, for the same reason as eks_cluster_name."
  type = object({
    namespace       = string
    service_account = string
  })
  default = {
    namespace       = ""
    service_account = ""
  }
}

variable "pod_identity_trust_policy_json" {
  description = "NOT APPLIED, for the same reason as eks_cluster_name."
  type        = string
  default     = ""
}
