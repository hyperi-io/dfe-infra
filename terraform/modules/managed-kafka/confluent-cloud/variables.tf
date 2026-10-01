// The contract, as OpenTofu sees it. Every input is described in CONTRACT.md;
// the validation blocks here are the half a reader cannot get from the types.
//
// Confluent Cloud sizes and tunes the cluster itself, so several contract
// inputs arrive and are not applied. They are declared rather than dropped,
// because a body that refuses an input the contract names has changed the
// contract; what this implementation cannot pass on is named in the description
// and in CONTRACT.md.

variable "implementation" {
  description = "Which body of the managed-kafka contract the caller asked for. This one answers to confluent-cloud and refuses anything else, so a mis-wired root fails at plan rather than building the wrong Kafka."
  type        = string
  default     = "confluent-cloud"

  validation {
    condition     = var.implementation == "confluent-cloud"
    error_message = "This body implements confluent-cloud. An msk, redpanda-cloud or strimzi deployment calls a different one."
  }
}

variable "tier" {
  description = "Which Confluent Cloud cluster type carries the deployment. Freight is the default -- it autoscales on eCKUs, is the cheapest at the top band, and is private-networking-only. Enterprise is the answer where Freight's unpublished end-to-end latency rules it out. Basic exists for a cheap public proof and carries no private networking."
  type        = string
  default     = "freight"

  validation {
    condition     = contains(["freight", "enterprise", "basic"], var.tier)
    error_message = "tier must be freight, enterprise or basic. Dedicated is provisioned CKUs with no published rate, so this module does not offer it."
  }
}

variable "connectivity" {
  description = "private puts the cluster behind Confluent's own private networking -- a Private Network Interface on Freight, a PrivateLink attachment on Enterprise. public is the reachable-from-anywhere shape and has to be asked for."
  type        = string
  default     = "private"

  validation {
    condition     = contains(["private", "public"], var.connectivity)
    error_message = "connectivity must be private or public."
  }

  validation {
    // Freight has no public endpoint at all, and Basic has no private
    // networking. Neither refusal is ours -- both are the product's shape.
    condition     = !(var.tier == "freight" && var.connectivity == "public")
    error_message = "Confluent Freight is private-networking-only, so connectivity = public cannot be built on it. Use tier = enterprise or basic for a public endpoint."
  }

  validation {
    condition     = !(var.tier == "basic" && var.connectivity == "private")
    error_message = "Confluent Basic carries no private networking. Use tier = freight or enterprise for a private cluster, and basic only for a public proof."
  }
}

variable "region" {
  description = "The AWS region Confluent places the cluster in, in Confluent's own region-code spelling (matches AWS's for every region Confluent supports). Read only on connectivity = public: on private connectivity the region is discovered from the VPC's own provider configuration (data.aws_region), because a private attachment has to sit in the same region as the network it attaches to, and asking the caller to restate a fact the VPC already carries would let the two disagree. Basic has no VPC to discover it from, so this is that path's only source -- empty is accepted at plan (a soft check warns) but Confluent refuses an empty region at apply."
  type        = string
  default     = ""
}

variable "name" {
  description = "Name prefix for every resource this module creates, and the cluster's own display name."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9_-]{1,58}[a-zA-Z0-9]$", var.name))
    error_message = "name must start and end with an alphanumeric character and otherwise hold letters, digits, hyphens and underscores -- Confluent's own rule for an environment display name."
  }
}

variable "env" {
  description = "Deployment environment, for the names and descriptions that have to distinguish two deployments in one organisation."
  type        = string
}

variable "network" {
  description = "The cluster module's network output. On private connectivity the PrivateLink endpoint or the Private Network Interfaces land in the PRIVATE subnets of this VPC."
  type = object({
    vpc_id             = string
    cidr               = string
    azs                = list(string)
    private_subnet_ids = list(string)
    public_subnet_ids  = list(string)
  })

  validation {
    condition     = length(var.network.private_subnet_ids) == length(var.network.azs)
    error_message = "network needs one private subnet per availability zone -- the private attachment is built zone by zone."
  }

  validation {
    condition     = length(var.network.private_subnet_ids) >= 2
    error_message = "The private attachment needs a subnet in at least two availability zones."
  }
}

variable "client_cidrs" {
  description = "Address ranges allowed to reach the private attachment. Empty means the whole VPC the cluster module built, which is the private network the DFE pods already sit on."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.client_cidrs, "0.0.0.0/0")
    error_message = "0.0.0.0/0 is not an allowlist. Name the ranges that may reach the cluster, or leave this empty for the VPC."
  }
}

variable "service_account_name" {
  description = "The one principal the DFE fleet authenticates as, matching the single dfe-kafka-user the Strimzi, Redpanda and MSK paths already grant. Confluent's own principal is the service account's sa- id; this is its display name."
  type        = string
  default     = "dfe-kafka-user"
}

variable "landing_topics" {
  description = "The topics created before DFE starts, keyed by topic name. dfe-loader treats a missing *_land topic as fatal, so every path pre-creates them rather than leaving it to the first produce. A null partition count or retention takes the tuning value below."
  type = map(object({
    partitions   = optional(number)
    retention_ms = optional(number)
  }))
  default = {}
}

variable "consumer_group_prefix" {
  description = "The consumer groups the DFE principal may read. The on-prem grant fences groups to this prefix rather than allowing all of them, and this body matches it."
  type        = string
  default     = "dfe-"
}

variable "private_network_interfaces_per_zone" {
  description = "How many elastic network interfaces the Freight Private Network Interface attachment takes in each zone. Confluent documents 17 per subnet, 51 across three zones, sized so the network layer does not cap a scaling operation. Ignored when private_network_interface_ids is supplied."
  type        = number
  default     = 17

  validation {
    condition     = var.private_network_interfaces_per_zone >= 1
    error_message = "private_network_interfaces_per_zone must be at least 1."
  }
}

variable "private_network_interface_ids" {
  description = "Interfaces the caller has already created and already granted Confluent permission to attach. Empty is the normal path and this module creates them. Supplying them splits the Freight attachment across two applies, which is the way out if the access point cannot take interface IDs that are still unknown at plan."
  type        = list(string)
  default     = []

  validation {
    // Confluent refuses an access point with fewer than six interfaces, and the
    // attribute is a SET, so six DISTINCT IDs is the floor.
    condition     = length(var.private_network_interface_ids) == 0 || length(distinct(var.private_network_interface_ids)) >= 6
    error_message = "private_network_interface_ids must be empty or carry at least six distinct interface IDs -- Confluent's own floor for an access point."
  }
}

variable "num_partitions" {
  description = "Default partitions for a landing topic that does not name its own. Derived by the chart from the same rule (S1.T7): a highly divisible multiple of the broker count, at least the consumer-parallelism ceiling. No default: the caller's dial states it, so a value cannot drift from msk's and redpanda-cloud's copies of the same setting silently."
  type        = number

  validation {
    condition     = var.num_partitions > 0
    error_message = "num_partitions must be positive."
  }
}

variable "log_retention_ms" {
  description = "How long a landing topic keeps data by default, in milliseconds. The buffer has to survive the longest consumer outage plus the archiver's lag; 259200000 is three days. No default: the caller's dial states it, so a value cannot drift from msk's and redpanda-cloud's copies of the same setting silently."
  type        = number

  validation {
    condition     = var.log_retention_ms > 0
    error_message = "log_retention_ms must be positive."
  }
}

variable "message_max_bytes" {
  description = "The largest record the topics accept, and the same number the scalo producer and consumer carry. Applied per topic as max.message.bytes, because Confluent Cloud holds the broker-wide setting. Confluent caps it per cluster type -- 8,388,608 on basic, 20,971,520 on enterprise and freight -- and the check block in main.tf says so at plan. No default: the caller's dial states it, so a value cannot drift from msk's and redpanda-cloud's copies of the same setting silently."
  type        = number

  validation {
    condition     = var.message_max_bytes > 0
    error_message = "message_max_bytes must be positive."
  }
}

variable "stream_governance_package" {
  description = "The schema registry tier the environment gets. ESSENTIALS is the free one and the only one DFE needs; ADVANCED buys catalogue and lineage nothing here reads."
  type        = string
  default     = "ESSENTIALS"

  validation {
    condition     = contains(["ESSENTIALS", "ADVANCED"], var.stream_governance_package)
    error_message = "stream_governance_package must be ESSENTIALS or ADVANCED -- the only two packages Confluent offers."
  }
}

// ---------------------------------------------------------------------------
// Contract inputs Confluent Cloud manages for us
// ---------------------------------------------------------------------------

variable "broker_shape_ref" {
  description = "NOT APPLIED. Confluent Cloud sells eCKUs, not instances, and exposes no instance type -- so the resolver's shape has nothing to select here."
  type        = string
  default     = ""
}

variable "resolved_shapes" {
  description = "NOT APPLIED, for the same reason as broker_shape_ref. A Dedicated cluster would map this onto a CKU count, which is why the input stays in the contract."
  type = map(object({
    instance_types = list(string)
    arch           = string
  }))
  default = {}
}

variable "broker_count" {
  description = "NOT APPLIED. The elastic tiers have no broker count to set -- Confluent scales eCKUs and bills throughput."
  type        = number
  default     = 3
}

variable "kafka_version" {
  description = "NOT APPLIED. Confluent Cloud exposes no Kafka version: it runs Kora with Kafka-protocol compatibility, so the hard deck (S0.T3) binds on-prem Strimzi and MSK and not this body."
  type        = string
  default     = ""
}

variable "kms_key_arn" {
  description = "NOT APPLIED. A customer-managed key on Confluent Cloud is a byok_key block on a Dedicated cluster, which this module does not offer; the elastic tiers encrypt at rest with Confluent's own key."
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
