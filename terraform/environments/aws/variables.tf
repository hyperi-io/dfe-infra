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
  description = "EKS control-plane minor version, pinned in tfvars (the dial's kubernetes_version) because the add-on pins in eks_addon_versions are built for one minor."
  type        = string
}

variable "eks_addon_versions" {
  description = "The EKS add-on versions, keyed by add-on name. Pinned in tfvars: render_dial.py renders versions.yaml's eks-addons, because a version EKS publishes after review must not reach a deploy. A kubernetes_version on another minor overrides all five in a later-loading tfvars file, and the plan refuses a kube-proxy built for another minor."
  type        = map(string)
}

variable "node_pools" {
  description = "Managed node groups, keyed by pool name, written by scripts/resolve_sizing.py: the dial's own pools plus the ones it derives. shape_ref indexes resolved_shapes; an instance type is never written here."
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
  description = "Auto-selected by the shape resolver under each shape's generation policy in shapes/compute-shapes.yaml: the newest arm64 generation in the region, or the generation the shape pins. Every node pool shape is pinned to Graviton4 (generation 8) while aws-network-policy-agent #686 leaves pod traffic timing out on Graviton5 nodes, and compute-shapes.yaml names the unpin trigger beside each pin. scripts/resolve_sizing.py reads the region's live EC2 and Pricing APIs against shapes/compute-shapes.yaml and writes this into sizing.auto.tfvars.json. Every node pool, the MSK broker, the toolbox and the tunnel forwarder take their instance type from here, and every shape with a root disk carries its root_volume type and size. One region's instance-generation availability is never another's."
  type = map(object({
    instance_types = list(string)
    arch           = string
    root_volume = optional(object({
      type     = string
      size_gib = number
    }))
  }))
}

variable "network" {
  description = "From tfvars (the dial's network block). nat = per-az at the scale tier, single below it. az_count is how many availability zones the VPC spans, 2-6, up to the region's own ceiling. subnet_newbits is how many bits each subnet adds to provision.cidr, which sets how many addresses each zone's subnets carry for nodes and pods."
  type = object({
    nat            = string
    az_count       = number
    subnet_newbits = number
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
  description = "The edge module (terraform/modules/edge/aws) -- the AWS Load Balancer Controller's identity, the public Route 53 zone, the external-dns and cert-manager identities that write it, and the fleet tunnel's own address. enabled is the whole-module switch the dial's edge.enabled feeds, on by default because a deployment with no door reaches nothing from outside the cluster. Turning it off AFTER a load balancer exists ORPHANS that load balancer, since the controller that owns it is gone: destroy the Services first, then disable. tunnel is the dial's edge.ingest.tunnel block; its address.mode defaults to byo, so nothing here is created until a deployment asks for the forwarder. address.shape_ref names the resolved shape the forwarder takes its instance type and root volume from, auto-selected by the shape resolver as the newest arm64 generation in the region. tunnel.admin_peer is the operator's reach-back to one appliance, and only its two fields travel here because they are a security-group rule on the toolbox -- the rest of the class is a chart value. client_cidr is the range appliances are issued out of, the culvert chart's vpn.clientCIDR, which that reach-back egress is aimed at. receiver.eips is the dial's edge.aws.receiver_eips: one Elastic IP per public subnet for the receiver's NLB, on by default. Every field is described in the module's own variables.tf."
  type = object({
    enabled = optional(bool, true)
    tunnel = optional(object({
      address = optional(object({
        mode      = optional(string, "byo")
        shape_ref = optional(string)
        zone      = optional(string, "")
      }), {})
      client_cidr   = optional(string)
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
    receiver = optional(object({
      eips = optional(bool, true)
    }), {})
  })

  default = {}

  // The forwarder's type and root volume come off a resolved shape, so mode forwarder needs one the resolver wrote.
  validation {
    condition     = var.edge.tunnel.address.mode != "forwarder" || try(var.resolved_shapes[var.edge.tunnel.address.shape_ref].root_volume != null, false)
    error_message = "edge.tunnel.address.mode is forwarder, so edge.tunnel.address.shape_ref must name a shape with a root volume in resolved_shapes -- render_dial.py sends the toolbox shape unless the dial names another, and scripts/resolve_sizing.py resolves it."
  }

  // The toolbox's reach-back egress is aimed at the range appliances are issued out of.
  validation {
    condition     = !var.edge.tunnel.admin_peer.enabled || can(cidrnetmask(var.edge.tunnel.client_cidr))
    error_message = "edge.tunnel.admin_peer is enabled, so edge.tunnel.client_cidr must be the tunnel's client range as a CIDR -- render_dial.py sends the culvert chart's own vpn.clientCIDR unless the dial names another."
  }
}

variable "edge_self_ranges" {
  description = "Addresses beyond the NAT gateways that this deployment's own tooling reaches the public gateway from: a guarded test run sets its runner's /32 here in an overlay, which is why it is a variable of its own (an overlay replaces a whole variable). Joins the NAT addresses in the gateway chart's exposure.selfRanges, which every web exposure group admits. The deployer's allow lists are exposure.groups in the deploy repo, never this."
  type        = list(string)
  default     = []
}

variable "edge_trusted_proxy_cidrs" {
  description = "L7 proxies in front of the public gateway whose X-Forwarded-For names the client, such as a CDN's, from the dial's edge.product.trusted_proxy_cidrs. Empty trusts no forwarded header."
  type        = list(string)
  default     = []
}

variable "telemetry" {
  description = <<-DESCRIPTION
    From tfvars (the dial's telemetry block). DFE's monitoring goes to its own
    OTel feed and HyperDX, never CloudWatch -- sink = otel is that policy.
    Where a managed AWS service forces a CloudWatch log group anyway (EKS's
    control-plane audit log, the MSK broker scaler's Lambda log), it is kept
    at forced_retention_days rather than removed, because there is nowhere
    else for AWS to put it. sink = cloudwatch is the OPT-IN AWS-native path
    for a compliance need CloudWatch itself satisfies, and keeps every
    touchpoint at retention_days instead. Passed unchanged to the cluster
    module (the EKS audit log) and to the msk module (the broker logs) --
    confluent-cloud and redpanda-cloud take no telemetry input, since neither
    emits a CloudWatch-shaped broker log.
  DESCRIPTION

  type = object({
    sink                  = string
    retention_days        = number
    forced_retention_days = number
  })

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

variable "lambda_runtimes" {
  description = "The Lambda runtimes the modules deploy, keyed by function. Pinned in tfvars: render_dial.py renders versions.yaml's lambda-runtimes, because a runtime Lambda publishes after review must not reach a deploy. msk-broker-scaler is the MSK broker scaler's, read only when kafka.provider is msk."
  type        = map(string)
}

variable "kafka" {
  description = "Who runs the brokers. strimzi and redpanda run inside the cluster and this root creates nothing for them; msk, confluent-cloud and redpanda-cloud are the managed bodies it does create. num_partitions, log_retention_ms, message_max_bytes and landing_topics are read for whichever managed body is selected -- every one applies the SAME canonical tuning (managed-kafka/CONTRACT.md). The msk block is read only when provider is msk and carries what is msk-only: the broker shape, count, version, SCRAM user and bootstrap Job, none of which the module defaults for a deployment that arrives through the renderer. confluent-cloud and redpanda-cloud take no such block: both size, version and tune the cluster themselves. Beyond name, env, network and the shared tuning, each takes connectivity (private unless the dial asks for public), confluent-cloud its tier, the deployment's region, an optional standing environment and the REST endpoint wait, and redpanda-cloud its tier and an optional standing resource group. Both tiers are pinned in tfvars, since neither vendor publishes a newer generation to resolve. A message_max_bytes above 8388608 is refused on a Confluent basic or standard tier, because Confluent refuses the topics at apply, after the cluster exists."
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
      // render_dial.py always sends the ceiling, the per-broker capacity, the
      // headroom and the alarm window, from the dial or its own documented
      // values; step left null takes the zone count in the module.
      autoscaling = object({
        enabled                  = optional(bool, true)
        max_brokers              = number
        step                     = optional(number)
        per_broker_capacity_mb_s = number
        headroom                 = number
        period_seconds           = number
        evaluation_periods       = number
      })
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
    // Read only for the two SaaS bodies. Private connectivity has the topics and
    // ACLs written from inside the VPC, so a run from outside it asks for public.
    connectivity = optional(string, "private")
    // Read only when provider is confluent-cloud, which render_dial.py always
    // sends a tier for. An empty environment id has the body create one of its own.
    confluent_tier           = optional(string)
    confluent_environment_id = optional(string, "")
    // Read only when provider is confluent-cloud, which render_dial.py always
    // sends the REST endpoint wait for.
    confluent_rest_ready = optional(object({
      successes        = number
      timeout_seconds  = number
      interval_seconds = number
    }))
    // Read only when provider is redpanda-cloud, which render_dial.py always
    // sends a tier for. An empty id has the body create a resource group of its own.
    redpanda_tier              = optional(string)
    redpanda_resource_group_id = optional(string, "")
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

  validation {
    condition     = var.kafka.provider != "confluent-cloud" || var.kafka.confluent_tier != null
    error_message = "kafka.provider is confluent-cloud, so kafka.confluent_tier must name the cluster type -- render_dial.py sends freight unless the dial names another."
  }

  validation {
    condition     = var.kafka.provider != "confluent-cloud" || var.kafka.confluent_rest_ready != null
    error_message = "kafka.provider is confluent-cloud, so kafka.confluent_rest_ready must carry successes, timeout_seconds and interval_seconds for the REST endpoint wait -- render_dial.py sends its documented values unless the dial names others."
  }

  validation {
    condition     = var.kafka.provider != "redpanda-cloud" || var.kafka.redpanda_tier != null
    error_message = "kafka.provider is redpanda-cloud, so kafka.redpanda_tier must name the product -- render_dial.py sends serverless unless the dial names another."
  }

  validation {
    // Confluent caps a topic's max.message.bytes at 8388608 on Basic and Standard,
    // and refuses a larger topic at apply, after the cluster has been created.
    condition = !(
      var.kafka.provider == "confluent-cloud"
      && contains(["basic", "standard"], var.kafka.confluent_tier == null ? "" : var.kafka.confluent_tier)
      && coalesce(var.kafka.message_max_bytes, 0) > 8388608
    )
    error_message = "kafka.message_max_bytes is ${coalesce(var.kafka.message_max_bytes, 0)}, above the 8388608 a Confluent ${var.kafka.confluent_tier == null ? "" : var.kafka.confluent_tier} cluster accepts for a topic. Lower the dial's kafka.msk.message_max_bytes to 8388608 or less, or set kafka.confluent_tier to freight or enterprise, which carry 20971520."
  }
}

// ---------------------------------------------------------------------------
// Toolbox
// ---------------------------------------------------------------------------

variable "toolbox" {
  description = "The on-demand SSM-managed troubleshooting instance (terraform/modules/toolbox/aws), from tfvars (the dial's toolbox block). enabled is the dfe-ops bastion up/down toggle -- down means the instance, its security group, its IAM role and both kinds of Session document do not exist (terminate, never stop); the session-log bucket is not gated by it (CONTRACT.md). aws.shape_ref names the resolved shape the instance takes its type and root volume from, auto-selected by the shape resolver as the newest arm64 generation in the region. aws.operator_role_arn is the IAM role granted a read-only EKS access entry when enabled -- required then, since a toolbox with no EKS identity to hand its kubectl forward to is not a deliverable. tool_versions is NOT a dial field a deployer sets by hand: render_dial.py assembles it from versions.yaml (toolbox/aws/CONTRACT.md), so it defaults empty here and the module's own validation refuses an enabled toolbox until it is populated."
  type = object({
    enabled = bool
    aws = object({
      shape_ref         = string
      operator_role_arn = optional(string, "")
    })
    ttl_minutes = number
    session = object({
      idle_timeout_minutes = number
      max_duration_minutes = number
    })
    session_log_retention_days = number
    tool_versions              = optional(map(string), {})
  })

  validation {
    condition     = !var.toolbox.enabled || var.toolbox.aws.operator_role_arn != ""
    error_message = "toolbox.enabled is true, so toolbox.aws.operator_role_arn must name the IAM role the EKS read-only access entry binds to."
  }

  // The module is always instantiated, so its type and root volume are read whether or not it is enabled.
  validation {
    condition     = contains(keys(var.resolved_shapes), var.toolbox.aws.shape_ref) && try(var.resolved_shapes[var.toolbox.aws.shape_ref].root_volume != null, false)
    error_message = "toolbox.aws.shape_ref must name a shape with a root volume in resolved_shapes -- re-run scripts/resolve_sizing.py, which resolves the toolbox shape on every populated cloud."
  }
}

// ---------------------------------------------------------------------------
// Registry cache
// ---------------------------------------------------------------------------

variable "registry_cache" {
  description = "The ECR pull-through cache every node pulls its images through (terraform/modules/registry-cache), from tfvars (the dial's registry_cache block). render_dial.py renders upstreams: the registries ECR caches with no credential (quay.io, registry.k8s.io, public.ecr.aws), plus docker.io or ghcr.io only where the dial names a Secrets Manager credential for it; an upstream that requires one and names none is skipped, and nodes pull it directly. namespace prefixes every rule and cached repository, and empty takes name. pull_expiry_days is how long a cached image outlives its last pull. enabled defaults to true, and false creates nothing. On with no upstreams creates nothing either."
  type = object({
    enabled          = optional(bool, true)
    namespace        = optional(string, "")
    pull_expiry_days = optional(number)
    upstreams = optional(map(object({
      upstream_url        = string
      host                = string
      requires_credential = bool
      credential_arn      = optional(string, "")
    })), {})
  })
  default  = {}
  nullable = false

  validation {
    condition     = !var.registry_cache.enabled || length(var.registry_cache.upstreams) == 0 || var.registry_cache.pull_expiry_days != null
    error_message = "registry_cache names upstreams, so pull_expiry_days must say how long a cached image outlives its last pull -- render_dial.py sends one."
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
  description = "In-cluster service addresses the DFE stack resolves each other by. kafka_bootstrap is the in-cluster broker on a strimzi or redpanda deployment and empty on a brokerless profile; on kafka.provider msk the module's own bootstrap replaces it. clickhouse_host is the external server on clickhouse.mode external, and becomes a toolbox forward target only when it names an address outside the cluster -- a Kubernetes Service name resolves through CoreDNS, which the toolbox instance cannot reach, so an in-cluster ClickHouse is port-forwarded over the eks-api target instead."
  type = object({
    clickhouse_host = string
    kafka_bootstrap = string
    otel_endpoint   = string
  })
}

variable "clickhouse" {
  description = "Where ClickHouse runs. in_cluster (the default) deploys DFE's own server from the clickhouse-cluster chart and reads nothing else here. external connects to a server the deployer brought, ClickHouse Cloud first: its host is endpoints.clickhouse_host, port is its HTTP(S) port, native_port its native protocol port, secure says whether both speak TLS, and user is the account the apps and the collector connect as. The defaults are ClickHouse Cloud's. The password is never here: it is clickhouse_external_password."
  type = object({
    mode        = optional(string, "in_cluster")
    port        = optional(number, 8443)
    native_port = optional(number, 9440)
    secure      = optional(bool, true)
    user        = optional(string, "default")
  })
  default = {}

  validation {
    condition     = contains(["in_cluster", "external"], var.clickhouse.mode)
    error_message = "clickhouse.mode must be in_cluster or external."
  }

  validation {
    condition = alltrue([
      for port in [var.clickhouse.port, var.clickhouse.native_port] : port == floor(port) && port >= 1 && port <= 65535
    ])
    error_message = "clickhouse.port and clickhouse.native_port must be whole port numbers between 1 and 65535."
  }

  validation {
    condition     = trimspace(var.clickhouse.user) != ""
    error_message = "clickhouse.user must name the account the apps connect as."
  }

  // A Service name resolves through CoreDNS only, so an external server it names
  // is one no pod could reach.
  validation {
    condition = var.clickhouse.mode != "external" || (
      strcontains(var.endpoints.clickhouse_host, ".")
      && !endswith(var.endpoints.clickhouse_host, ".cluster.local")
      && !endswith(var.endpoints.clickhouse_host, ".svc")
    )
    error_message = "clickhouse.mode is external, so endpoints.clickhouse_host must name the external server -- a dotted host name outside the cluster."
  }
}

variable "clickhouse_external_password" {
  description = "The external ClickHouse's password, set as TF_VAR_clickhouse_external_password and never in a tfvars file. The secrets module seeds it at <project>/<env>/clickhouse/external, where the clickhouse-cluster chart reads it. Required when clickhouse.mode is external, unread otherwise. Only the first apply writes it: the store keeps that value."
  type        = string
  sensitive   = true
  default     = null

  validation {
    condition     = var.clickhouse.mode != "external" || (var.clickhouse_external_password != null ? var.clickhouse_external_password != "" : false)
    error_message = "clickhouse.mode is external, so TF_VAR_clickhouse_external_password must hold the external server's password."
  }
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

// ---------------------------------------------------------------------------
// Test runs and account guardrails -- none of these is a dial field. A guarded
// test run (`dfe-ops cloud-cycle`) sets them in its own overlay tfvars file;
// every other deployment leaves them at their defaults and changes nothing.
// ---------------------------------------------------------------------------

variable "run" {
  description = "The test run this apply belongs to, or null. Its id and expiry are added to every resource's tags at create, so a reaper can remove what a stalled run left behind. Shape and validation: terraform/modules/tf-run-tags."
  type        = any
  default     = null
}

variable "permissions_boundary" {
  description = "IAM policy ARN set as the permissions boundary on EVERY role this root and its modules create, or null for none. An account whose guardrail denies CreateRole without a boundary needs it; any other leaves it null."
  type        = string
  default     = null

  validation {
    condition     = var.permissions_boundary == null || can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:policy/.+$", var.permissions_boundary))
    error_message = "permissions_boundary must be an IAM policy ARN (arn:<partition>:iam::<account>:policy/<name>) or null."
  }
}

variable "iam_path" {
  description = "IAM path for EVERY role, policy and instance profile this root and its modules create, or null for the default \"/\". An account whose guardrail lets a runner create, pass or modify IAM only under one path needs it; changing it on an existing deployment replaces every role."
  type        = string
  default     = null

  validation {
    condition     = var.iam_path == null || can(regex("^/([A-Za-z0-9+=,.@_-]+/)*$", var.iam_path))
    error_message = "iam_path must begin and end with '/' and hold only letters, digits and + = , . @ _ - between them (e.g. /dfe-e2e/), or be null."
  }
}

variable "s3_bucket_prefix" {
  description = "Prepended to the name of EVERY bucket this root and its modules create. Empty by default; an account whose guardrail scopes S3 writes to a name prefix sets it. Changing it on an existing deployment replaces every bucket."
  type        = string
  default     = ""

  validation {
    condition     = var.s3_bucket_prefix == "" || can(regex("^[a-z0-9][a-z0-9.-]*$", var.s3_bucket_prefix))
    error_message = "s3_bucket_prefix must start with a lowercase letter or digit and hold only lowercase letters, digits, '.' and '-', or be empty."
  }
}

variable "inspector_ec2_exclusion" {
  description = "Adds Amazon Inspector's InspectorEc2Exclusion tag to everything the run tags reach, so a short-lived deployment's instances, and volumes under its key, are not scanned and not charged for. Off by default."
  type        = bool
  default     = false
}

variable "cloudtrail" {
  description = "enabled builds the deployment's own trail, its bucket and its CloudWatch half. On by default, because a real deployment wants its audit record; a short-lived test run turns it off, since every apply would otherwise create one more trail in the account."
  type = object({
    enabled = optional(bool, true)
  })
  default = {}
}
