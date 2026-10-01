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
  description = "The shape resolver's committed answer for this cloud AND region, read from shapes/resolved/aws-<region>.json and diffed on every change. One region's instance-generation availability is never another's."
  type = map(object({
    instance_types = list(string)
    arch           = string
  }))
}

variable "network" {
  description = "nat = per-az at the scale tier, single below it. az_count is how many availability zones the VPC spans -- 2 for cost, 3 (the default) for the usual spread, up to the region's own ceiling."
  type = object({
    nat      = string
    az_count = optional(number, 3)
  })

  validation {
    condition     = var.network.az_count >= 2 && var.network.az_count <= 6
    error_message = "network.az_count must be between 2 and 6 -- below 2 there is no redundancy to speak of, and no AWS region offers more than 6."
  }
}

variable "endpoint" {
  description = "The private Kubernetes API endpoint is always on. public adds the public one for operators outside the VPC, restricted to allowed_cidrs."
  type = object({
    public        = bool
    allowed_cidrs = list(string)
  })
}

variable "dns" {
  description = "private_zone resolves inside the VPC and carries everything internal. public_zone is the delegated zone the exposed UIs answer on; empty means no public names. The private half goes to the cluster module and the public half to the edge module, which is what owns every resource that exists because something crosses the VPC boundary."
  type = object({
    private_zone = string
    public_zone  = string
  })
}

// ---------------------------------------------------------------------------
// Edge
// ---------------------------------------------------------------------------

variable "edge" {
  description = "The edge module (terraform/modules/edge/aws) -- the AWS Load Balancer Controller's identity, the public Route 53 zone, the external-dns and cert-manager identities that write it, and the fleet tunnel's own address. enabled is the whole-module switch the dial's edge.enabled feeds, on by default because a deployment with no door reaches nothing from outside the cluster. Turning it off AFTER a load balancer exists ORPHANS that load balancer, since the controller that owns it is gone: destroy the Services first, then disable. tunnel is the dial's edge.ingest.tunnel block; its address.mode defaults to byo, so nothing here is created until a deployment asks for the forwarder. tunnel.admin_peer is the operator's reach-back to one appliance, and only its two fields travel here because they are a security-group rule on the toolbox -- the rest of the class is a chart value. Every field is described in the module's own variables.tf."
  type = object({
    enabled = optional(bool, true)
    tunnel = optional(object({
      address = optional(object({
        mode          = optional(string, "byo")
        instance_type = optional(string, "t4g.small")
        zone          = optional(string, "")
      }), {})
      openvpn       = optional(bool, true)
      source_ranges = optional(list(string), [])
      node_ports = optional(object({
        wireguard = optional(number, 31820)
        openvpn   = optional(number, 31194)
      }), {})
      admin_peer = optional(object({
        enabled = optional(bool, true)
        reach   = optional(list(number), [22, 443])
      }), {})
    }), {})
  })

  default = {}
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
  description = "Who runs the brokers. strimzi and redpanda run inside the cluster and this root creates nothing for them; msk, confluent-cloud and redpanda-cloud are the managed bodies it does create. num_partitions, log_retention_ms, message_max_bytes and landing_topics are read for whichever managed body is selected -- every one applies the SAME canonical tuning (managed-kafka/CONTRACT.md). The msk block is read only when provider is msk and carries what is msk-only: the broker shape, count, version, SCRAM user and bootstrap Job, none of which the module defaults for a deployment that arrives through the renderer. confluent-cloud and redpanda-cloud take no such block: both size, version and tune the cluster themselves, so name, env, network and the shared tuning are all this root passes either one."
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
    // Read only when provider is confluent-cloud or redpanda-cloud -- msk's
    // equivalents live inside msk above, and its landing topics are its own
    // in-cluster bootstrap Job's (CONTRACT.md: neither SaaS body has one, so
    // tofu creates their topics itself).
    num_partitions    = optional(number)
    log_retention_ms  = optional(number)
    message_max_bytes = optional(number)
    landing_topics = optional(map(object({
      partitions   = optional(number)
      retention_ms = optional(number)
    })), {})
  })

  validation {
    condition     = contains(["strimzi", "redpanda", "msk", "confluent-cloud", "redpanda-cloud"], var.kafka.provider)
    error_message = "kafka.provider must be strimzi, redpanda, msk, confluent-cloud or redpanda-cloud. The first two run in the cluster; the other three are managed bodies this root builds."
  }

  validation {
    condition     = var.kafka.provider != "msk" || var.kafka.msk != null
    error_message = "kafka.provider is msk, so the kafka.msk block has to be present -- it carries the broker shape, the version and the tuning, none of which this root invents."
  }

  validation {
    condition = !contains(["confluent-cloud", "redpanda-cloud"], var.kafka.provider) || (
      var.kafka.num_partitions != null && var.kafka.log_retention_ms != null && var.kafka.message_max_bytes != null
    )
    error_message = "kafka.provider is a managed SaaS body, so num_partitions, log_retention_ms and message_max_bytes have to be present -- the module defaults none of them."
  }

  validation {
    // Neither SaaS body has a bootstrap Job of its own -- the vendor's
    // provider writes the topics -- so an empty map here is a cluster with no
    // landing topic, and dfe-loader treats a missing *_land topic as fatal.
    condition     = !contains(["confluent-cloud", "redpanda-cloud"], var.kafka.provider) || length(var.kafka.landing_topics) > 0
    error_message = "kafka.provider is a managed SaaS body with no bootstrap Job of its own, so landing_topics must name at least one topic -- an empty map ships a cluster dfe-loader crash-loops against."
  }
}

// ---------------------------------------------------------------------------
// Toolbox
// ---------------------------------------------------------------------------

variable "toolbox" {
  description = "The on-demand SSM-managed troubleshooting instance (terraform/modules/toolbox/aws). enabled is the dfe-ops bastion up/down toggle -- down means the instance, its security group, its IAM role and both kinds of Session document do not exist (terminate, never stop); the session-log bucket is not gated by it (CONTRACT.md). aws.operator_role_arn is the IAM role granted a read-only EKS access entry when enabled -- required then, since a toolbox with no EKS identity to hand its kubectl forward to is not a deliverable. tool_versions is NOT a dial field a deployer sets by hand: render_dial.py assembles it from versions.yaml (toolbox/aws/CONTRACT.md), so it defaults empty here and the module's own validation refuses an enabled toolbox until it is populated."
  type = object({
    enabled = bool
    aws = object({
      instance_type     = string
      operator_role_arn = optional(string, "")
    })
    ttl_minutes = number
    session = object({
      idle_timeout_minutes = number
      max_duration_minutes = number
    })
    session_log_retention_days = optional(number, 90)
    tool_versions              = optional(map(string), {})
  })

  validation {
    condition     = !var.toolbox.enabled || var.toolbox.aws.operator_role_arn != ""
    error_message = "toolbox.enabled is true, so toolbox.aws.operator_role_arn must name the IAM role the EKS read-only access entry binds to."
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
  description = "In-cluster service addresses the DFE stack resolves each other by. kafka_bootstrap is the in-cluster broker on a strimzi or redpanda deployment and empty on a brokerless profile; on kafka.provider msk the module's own bootstrap replaces it. clickhouse_host becomes a toolbox forward target only when it names an address outside the cluster -- a Kubernetes Service name resolves through CoreDNS, which the toolbox instance cannot reach, so an in-cluster ClickHouse is port-forwarded over the eks-api target instead."
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
