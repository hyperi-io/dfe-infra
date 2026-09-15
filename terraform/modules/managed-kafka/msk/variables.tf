// The contract, as OpenTofu sees it. Every input is described in CONTRACT.md;
// the validation blocks here are the half a reader cannot get from the types.

variable "implementation" {
  description = "Which body of the managed-kafka contract the caller asked for. This one answers to msk and refuses anything else, so a mis-wired root fails at plan rather than building the wrong Kafka."
  type        = string
  default     = "msk"

  validation {
    condition     = var.implementation == "msk"
    error_message = "This body implements msk. A redpanda-cloud, confluent-cloud or strimzi deployment calls a different one."
  }
}

variable "connectivity" {
  description = "private puts the brokers on the cluster's private subnets and nowhere else. MSK can be made publicly reachable only by a later update, never at create, so public is not a shape this body offers."
  type        = string
  default     = "private"

  validation {
    condition     = var.connectivity == "private"
    error_message = "connectivity must be private -- MSK refuses public access at cluster creation, so a public broker is a deliberate post-create change and not something this module builds."
  }
}

variable "name" {
  description = "Name prefix for every resource this module creates, and the MSK cluster's own name."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9-]{1,58}$", var.name))
    error_message = "name must be letters, digits and hyphens only, and short enough to leave room for the per-resource suffixes MSK appends."
  }
}

variable "env" {
  description = "Deployment environment, for the names and descriptions that have to distinguish two deployments in one account."
  type        = string
}

variable "network" {
  description = "The cluster module's network output. Brokers take the PRIVATE subnets, one per availability zone, and the security group below is created in the same VPC."
  type = object({
    vpc_id             = string
    cidr               = string
    azs                = list(string)
    private_subnet_ids = list(string)
    public_subnet_ids  = list(string)
  })

  validation {
    condition     = length(var.network.private_subnet_ids) >= 2
    error_message = "MSK needs a client subnet in at least two availability zones."
  }
}

variable "broker_shape_ref" {
  description = "Indexes resolved_shapes, exactly as a node pool does. MSK's broker namespace is its own and runs generations behind EC2, so the resolver answers it separately."
  type        = string
}

variable "resolved_shapes" {
  description = "The shape resolver's answer, keyed by shape_ref. instance_types is ordered best-first; MSK takes ONE type for the whole cluster, so this body uses the first and the rest are the resolver's fallbacks."
  type = map(object({
    instance_types = list(string)
    arch           = string
  }))

  validation {
    condition     = contains(keys(var.resolved_shapes), var.broker_shape_ref)
    error_message = "broker_shape_ref must have an entry in resolved_shapes."
  }

  validation {
    condition     = length(try(var.resolved_shapes[var.broker_shape_ref].instance_types, [])) > 0
    error_message = "The broker shape needs at least one instance type."
  }

  validation {
    // "express." is MSK's own prefix for the Express broker family; a kafka.*
    // type is a Standard broker, and MSK cannot convert a Standard cluster to
    // Express afterwards -- it has to be rebuilt.
    condition     = startswith(try(var.resolved_shapes[var.broker_shape_ref].instance_types[0], ""), "express.")
    error_message = "The broker shape must resolve to an MSK Express instance type (express.*). A kafka.* type builds a Standard cluster, which cannot be switched to Express later."
  }

  validation {
    condition     = try(var.resolved_shapes[var.broker_shape_ref].arch, "") == "arm64"
    error_message = "MSK Express brokers are ARM only. An x86 shape here means the resolver was asked for the wrong thing."
  }
}

variable "broker_count" {
  description = "Total brokers in the cluster. Three is the floor -- one per availability zone -- and MSK requires a multiple of the client subnet count so the zones stay evenly loaded."
  type        = number
  default     = 3

  validation {
    condition     = var.broker_count >= 3
    error_message = "broker_count must be at least 3 -- replication factor 3 and minimum in-sync replicas 2 need three brokers to survive losing one."
  }

  validation {
    condition     = var.broker_count % length(var.network.private_subnet_ids) == 0
    error_message = "broker_count must be a multiple of the number of client subnets, which is the number of availability zones the cluster module built."
  }
}

variable "kafka_version" {
  description = "MSK's own spelling of the broker version, e.g. 4.2.x.kraft. No default: the newest ACTIVE version is read from the MSK API at build time, because a default here would rot silently into a deprecated line."
  type        = string

  validation {
    // MSK names a KRaft version <major>.<minor>.x.kraft. The ZooKeeper spellings
    // (3.9.x, 3.6.0) are the ones without the suffix, and KRaft is the only
    // metadata plane this deployment supports.
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.x\\.kraft$", var.kafka_version))
    error_message = "kafka_version must be an MSK KRaft version string like 4.2.x.kraft. A version without the .kraft suffix is a ZooKeeper cluster."
  }
}

variable "client_cidrs" {
  description = "Address ranges allowed to reach the brokers. Empty means the whole VPC the cluster module built, which is the private network the DFE pods and the bootstrap Job already sit on."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.client_cidrs, "0.0.0.0/0")
    error_message = "0.0.0.0/0 is not an allowlist. Name the ranges that may reach the brokers, or leave this empty for the VPC."
  }
}

variable "kms_key_arn" {
  description = "The deployment's customer-managed key, from the cluster module. Encrypts the broker data at rest and the SCRAM secret -- MSK refuses a SCRAM secret on the default aws/secretsmanager key."
  type        = string
}

variable "num_partitions" {
  description = "Default partitions for a topic the brokers create. Derived by the chart from the same rule (S1.T7): a highly divisible multiple of the broker count, at least the consumer-parallelism ceiling, so a 3 -> 6 -> 12 scale-out divides evenly every time. No default: the caller's dial states it, so a value cannot drift from confluent-cloud's and redpanda-cloud's copies of the same setting silently."
  type        = number

  validation {
    condition     = var.num_partitions > 0
    error_message = "num_partitions must be positive."
  }
}

variable "log_retention_ms" {
  description = "How long a topic keeps data by default, in milliseconds. The buffer has to survive the longest consumer outage plus the archiver's lag; 259200000 is three days. No default: the caller's dial states it, so a value cannot drift from confluent-cloud's and redpanda-cloud's copies of the same setting silently."
  type        = number

  validation {
    condition     = var.log_retention_ms > 0
    error_message = "log_retention_ms must be positive -- MSK Express manages storage, so there is no size-based retention to fall back on."
  }
}

variable "message_max_bytes" {
  description = "The largest record the brokers accept, and the same number the replica fetcher, the topic and the scalo producer and consumer carry. 16 MiB covers filebeat's own 10 MiB truncation ceiling plus the ~1.48x enrichment growth measured on the fixtures. No default: the caller's dial states it, so a value cannot drift from confluent-cloud's and redpanda-cloud's copies of the same setting silently."
  type        = number

  validation {
    condition     = var.message_max_bytes > 0
    error_message = "message_max_bytes must be positive."
  }
}

variable "telemetry" {
  description = <<-DESCRIPTION
    Where the broker logs land. DFE's monitoring goes to its own OTel feed and
    HyperDX, never CloudWatch, so sink = otel (the default) delivers broker
    logs to an S3 bucket this module creates -- the fetcher's object-store
    source reads it into the same feed everything else lands in, and the
    bucket's own lifecycle bounds what it costs. sink = cloudwatch is the
    opt-in AWS-native path for a compliance need CloudWatch itself satisfies;
    kept minimal and short-retention, never the default. open_monitoring
    (Prometheus JMX + node exporter) stays on under both -- it is scraped
    in-cluster, not delivered through either sink.
  DESCRIPTION

  type = object({
    sink           = string
    retention_days = number
  })

  default = {
    sink           = "otel"
    retention_days = 2
  }

  validation {
    condition     = contains(["otel", "cloudwatch"], var.telemetry.sink)
    error_message = "telemetry.sink must be otel or cloudwatch."
  }

  validation {
    condition     = var.telemetry.retention_days > 0
    error_message = "telemetry.retention_days must be positive."
  }

  validation {
    // CloudWatch Logs takes a fixed set of retention periods and rejects
    // anything else. S3's own lifecycle expiration takes any positive day
    // count, so this only binds the cloudwatch sink.
    condition = var.telemetry.sink != "cloudwatch" || contains(
      [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653],
      var.telemetry.retention_days
    )
    error_message = "telemetry.retention_days must be one of the retention periods CloudWatch Logs accepts when sink is cloudwatch."
  }
}

variable "rebalancing_status" {
  description = "MSK's intelligent rebalancing, which spreads partitions onto brokers added later. ACTIVE by default on a new Express cluster, and asserted here so the state is declared rather than inherited; enabling it forecloses Cruise Control on the same cluster."
  type        = string
  default     = "ACTIVE"

  validation {
    condition     = contains(["ACTIVE", "PAUSED"], var.rebalancing_status)
    error_message = "rebalancing_status must be ACTIVE or PAUSED -- the only two values the MSK API takes."
  }
}

variable "scram_username" {
  description = "The one SCRAM principal the DFE fleet authenticates as, matching the single dfe-kafka-user the Strimzi and Redpanda paths already grant."
  type        = string
  default     = "dfe-kafka-user"
}

variable "scram_password" {
  description = "The SCRAM password, from the secrets module's kafka seed. Sensitive, and it reaches Kafka only through the AmazonMSK_ secret this module writes."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.scram_password) >= 8
    error_message = "MSK rejects a SCRAM password shorter than 8 characters."
  }
}

variable "secret_recovery_window_days" {
  description = "Secrets Manager's deletion delay. Defaults to the vendor's own 30 days -- a persistent deployment's default -- and the caller sets 0 explicitly for a throwaway deployment torn down and rebuilt under the same name, where the 30-day window would make the second create fail on a name that still exists."
  type        = number
  default     = 30
}

variable "eks_cluster_name" {
  description = "The EKS cluster whose bootstrap-Job service account gets the IAM identity, from the cluster module."
  type        = string
}

variable "pod_identity" {
  description = "Namespace and service account of the in-cluster bootstrap Job. The Job itself -- the ACLs and the landing topics -- is chart work; this module mints the identity it authenticates with."
  type = object({
    namespace       = string
    service_account = string
  })

  validation {
    condition     = length(var.pod_identity.namespace) > 0 && length(var.pod_identity.service_account) > 0
    error_message = "pod_identity needs both a namespace and a service account -- a Pod Identity association to a name nothing renders grants nothing, silently."
  }
}

variable "pod_identity_trust_policy_json" {
  description = "From the cluster module, so this module mints a role without restating how EKS expresses cluster trust."
  type        = string
}

variable "autoscaling" {
  description = <<-DESCRIPTION
    Broker-count scaling for the Express cluster. AWS gives MSK Express no
    native equivalent -- UpdateBrokerCount is a manual API with no scaling
    policy -- so this is a CloudWatch alarm on cluster-wide BytesInPerSec
    driving a Lambda that calls it. enabled defaults true; every other field
    is the caller's, because none of them has a value that is safe to guess:
      - max_brokers: the ceiling. Must be >= broker_count and a multiple of
        the client subnet count, exactly like broker_count itself.
      - step: brokers added per scale-out. Defaults to the client subnet
        count when unset, which is what keeps every step a multiple of the
        zone count the way MSK requires.
      - per_broker_capacity_mb_s: the resolved broker shape's published
        network throughput, in MB/s. Not derived here -- resolved_shapes
        carries instance_types and arch only, no throughput figure, so this
        is the caller's own reading of AWS's per-instance-type numbers.
      - headroom: multiplies the raw capacity ceiling (broker_count x
        per_broker_capacity_mb_s) to get the alarm threshold. A margin ABOVE
        full utilisation, so it is always greater than 1.
  DESCRIPTION

  type = object({
    enabled                  = optional(bool, true)
    max_brokers              = number
    step                     = optional(number)
    per_broker_capacity_mb_s = number
    headroom                 = number
  })

  validation {
    condition     = var.autoscaling.max_brokers >= var.broker_count
    error_message = "autoscaling.max_brokers must be at least broker_count -- a ceiling below the starting count forecloses scaling before it starts."
  }

  validation {
    condition     = var.autoscaling.max_brokers % length(var.network.private_subnet_ids) == 0
    error_message = "autoscaling.max_brokers must be a multiple of the client subnet count, exactly like broker_count -- MSK requires an even spread across zones at every step."
  }

  validation {
    condition     = var.autoscaling.per_broker_capacity_mb_s > 0
    error_message = "autoscaling.per_broker_capacity_mb_s must be positive -- it is what the alarm threshold scales with broker_count."
  }

  // AWS/Kafka publishes BytesInPerSec per broker and never as a cluster total,
  // and PutMetricAlarm rejects a SEARCH expression, so the alarm names one
  // metric per broker -- and a metric math expression takes at most ten.
  validation {
    condition     = !var.autoscaling.enabled || var.broker_count <= 10
    error_message = "autoscaling.enabled needs broker_count of 10 or fewer: the scale-out alarm names one CloudWatch metric per broker, and a metric math expression takes at most ten metrics. Refused at plan rather than at apply, where MSK, the Lambda, the SNS topic and the IAM all exist before PutMetricAlarm rejects it. Turn autoscaling off, or scale this cluster by hand."
  }

  validation {
    condition     = var.autoscaling.headroom > 1
    error_message = "autoscaling.headroom must be greater than 1 -- it is a margin ABOVE full utilisation, not a fraction of it."
  }
}
