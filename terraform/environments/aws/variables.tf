// The dial, as OpenTofu sees it. Nothing here carries a value that names a
// particular deployment -- those arrive in a tfvars file the deployer renders.

variable "provision" {
  description = "Which cloud, which account, which region, which network. The cloud token picks the root; the account is asserted against the caller's identity before anything is created."
  type = object({
    cloud   = string
    account = string
    region  = string
    cidr    = string
  })

  validation {
    condition     = var.provision.cloud == "aws"
    error_message = "This root is the aws one. provision.cloud must be aws -- gcp and azure have roots of their own."
  }
}

variable "name" {
  description = "Name prefix for every resource this deployment creates."
  type        = string
}

variable "env" {
  description = "Deployment environment: dev, test, staging, prod or customer-<id>."
  type        = string
}

variable "profile" {
  description = "Deployment profile: slim (single node, gRPC, no Kafka), single (single node, with Kafka), scale (HA, with Kafka) or mesh (HA, gRPC, no Kafka)."
  type        = string

  validation {
    condition     = contains(["slim", "single", "scale", "mesh"], var.profile)
    error_message = "profile must be slim, single, scale or mesh."
  }
}

// ---------------------------------------------------------------------------
// Cluster
// ---------------------------------------------------------------------------

variable "kubernetes_version" {
  description = "EKS control-plane minor version."
  type        = string
}

variable "node_pools" {
  description = "Managed node groups, keyed by pool name. shape_ref indexes resolved_shapes; an instance type is never written here."
  type = map(object({
    shape_ref     = string
    min_size      = number
    max_size      = number
    desired_size  = number
    capacity_type = string
    disk_gb       = number
    labels        = map(string)
    taints = list(object({
      key    = string
      value  = string
      effect = string
    }))
  }))
}

variable "resolved_shapes" {
  description = "The shape resolver's committed answer for this cloud, read from shapes/resolved/aws.json and diffed on every change."
  type = map(object({
    instance_types = list(string)
    arch           = string
  }))
}

variable "network" {
  description = "nat = per-az at the scale tier, single below it."
  type = object({
    nat = string
  })
}

variable "endpoint" {
  description = "The private Kubernetes API endpoint is always on. public adds the public one for operators outside the VPC, restricted to allowed_cidrs."
  type = object({
    public        = bool
    allowed_cidrs = list(string)
  })
}

variable "dns" {
  description = "private_zone resolves inside the VPC and carries everything internal. public_zone is the delegated zone the exposed UIs answer on; empty means no public names."
  type = object({
    private_zone = string
    public_zone  = string
  })
}

variable "telemetry" {
  description = <<-DESCRIPTION
    DFE's monitoring goes to its own OTel feed and HyperDX, never CloudWatch --
    sink = otel (the default) is that policy. Where a managed AWS service
    forces a CloudWatch-shaped touchpoint anyway (MSK's broker logs, EKS's
    control-plane audit log), it is kept minimal and short-retention rather
    than removed, because there is nowhere else for AWS to put it.
    sink = cloudwatch is the OPT-IN AWS-native path for a compliance need
    CloudWatch itself satisfies, and keeps every touchpoint at retention_days
    instead. Passed unchanged to the cluster module (the EKS audit log) and to
    the msk module (the broker logs) -- confluent-cloud and redpanda-cloud take
    no telemetry input, since neither emits a CloudWatch-shaped broker log.
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
}

variable "storage_class" {
  description = "StorageClass every DFE PersistentVolumeClaim asks for."
  type        = string
}

// ---------------------------------------------------------------------------
// Kafka
// ---------------------------------------------------------------------------

variable "kafka" {
  description = "Who runs the brokers. strimzi and redpanda run inside the cluster and this root creates nothing for them; msk, confluent-cloud and redpanda-cloud are the managed bodies it does create. The msk block is read only when provider is msk, and its values are the dial's -- the module defaults none of them for a deployment that arrives through the renderer. confluent-cloud and redpanda-cloud take no block of their own: both size and tune the cluster themselves (managed-kafka/CONTRACT.md), so name, env and network are all this root passes either one."
  type = object({
    provider = string
    msk = optional(object({
      shape_ref         = string
      broker_count      = number
      broker_version    = string
      num_partitions    = number
      log_retention_ms  = number
      message_max_bytes = number
      scram_username    = string
      bootstrap_job = object({
        namespace       = string
        service_account = string
      })
      // Every attribute here is optional with a module-level default, so a
      // dial that names no autoscaling block still gets one -- the msk module
      // itself demands the object, but nothing demands the dial mention it.
      autoscaling = optional(object({
        enabled                  = optional(bool, true)
        max_brokers              = optional(number, 6)
        step                     = optional(number)
        per_broker_capacity_mb_s = optional(number, 50)
        headroom                 = optional(number, 1.3)
      }), {})
    }))
  })

  validation {
    condition     = contains(["strimzi", "redpanda", "msk", "confluent-cloud", "redpanda-cloud"], var.kafka.provider)
    error_message = "kafka.provider must be strimzi, redpanda, msk, confluent-cloud or redpanda-cloud. The first two run in the cluster; the other three are managed bodies this root builds."
  }

  validation {
    condition     = var.kafka.provider != "msk" || var.kafka.msk != null
    error_message = "kafka.provider is msk, so the kafka.msk block has to be present -- it carries the broker shape, the version and the tuning, none of which this root invents."
  }
}

// ---------------------------------------------------------------------------
// Secrets
// ---------------------------------------------------------------------------

variable "secrets" {
  description = "backend names the store body; ref is its path prefix."
  type = object({
    backend = string
    ref     = string
  })

  validation {
    condition     = var.secrets.backend == "aws-sm"
    error_message = "This root composes the aws-sm secrets body. gcp-sm and azure-kv belong to their own roots."
  }
}

variable "seeds" {
  description = "Credentials the deploy layer puts in the store. Secret name -> field -> value; an empty value is generated and never surfaces -- in the secrets module, or in this root for the Kafka password a managed broker is created with."
  type        = map(map(string))
}

// ---------------------------------------------------------------------------
// What the deployment points at
// ---------------------------------------------------------------------------

variable "endpoints" {
  description = "In-cluster service addresses the DFE stack resolves each other by. kafka_bootstrap is the in-cluster broker on a strimzi or redpanda deployment and empty on a brokerless profile; on kafka.provider msk the module's own bootstrap replaces it."
  type = object({
    clickhouse_host = string
    kafka_bootstrap = string
    otel_endpoint   = string
  })
}

variable "repo_url" {
  description = "Git repo Argo CD syncs the stack from."
  type        = string
}

variable "target_revision" {
  description = "Git branch or tag Argo CD tracks."
  type        = string
}

variable "registry_host" {
  description = "Container registry hostname. Empty skips the pull secret."
  type        = string
  default     = ""
}

variable "registry_user" {
  description = "Container registry username."
  type        = string
  default     = ""
}

variable "registry_token" {
  description = "Container registry token."
  type        = string
  sensitive   = true
  default     = ""
}

// ---------------------------------------------------------------------------
// State and tags
// ---------------------------------------------------------------------------

variable "state" {
  description = "Where this root's state lives. Read by the backend block at init, which is why every field is a plain string with no default."
  type = object({
    bucket = string
    key    = string
    region = string
  })
}

variable "tags" {
  description = "The governance tag set, applied to every taggable resource through the provider's default_tags."
  type        = map(string)

  validation {
    condition = alltrue([
      for k in ["service-name", "service-namespace", "environment", "owner", "cost-center", "lifecycle", "iac-source"] :
      lookup(var.tags, k, "") != ""
    ])
    error_message = "tags must carry service-name, service-namespace, environment, owner, cost-center, lifecycle and iac-source, each non-empty."
  }
}
