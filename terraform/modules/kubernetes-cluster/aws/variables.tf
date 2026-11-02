// The contract, as OpenTofu sees it. Every input is described in CONTRACT.md;
// the validation blocks here are the half a reader cannot get from the types.

variable "provision" {
  description = "Where the cluster goes: the AWS account id it must land in, the region, and the VPC CIDR the module subdivides."
  type = object({
    account = string
    region  = string
    cidr    = string
  })

  validation {
    condition     = can(regex("^[0-9]{12}$", var.provision.account))
    error_message = "provision.account must be a 12-digit AWS account id."
  }

  // The region is interpolated into the destroy-time local-execs (dns.tf's
  // private zone, eks.tf's CNI network interfaces), which run under the
  // destroying operator's own credentials, so it is shaped here rather than
  // trusted.
  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[0-9]$", var.provision.region))
    error_message = "provision.region must be an AWS region code such as us-west-2 or ap-southeast-2."
  }

  validation {
    condition     = can(cidrsubnet(var.provision.cidr, var.network.subnet_newbits, pow(2, var.network.subnet_newbits) - 1))
    error_message = "provision.cidr must be a CIDR block with room for 2^network.subnet_newbits subnets."
  }

  validation {
    condition     = !startswith(var.provision.cidr, "172.31.")
    error_message = "provision.cidr overlaps the default VPC every AWS account is created with (172.31.0.0/16)."
  }
}

variable "name" {
  description = "Name prefix for every resource this module creates."
  type        = string
}

variable "env" {
  description = "Deployment environment, for the names that have to distinguish two deployments in one account."
  type        = string
}

variable "kubernetes_version" {
  description = "EKS control-plane minor version, e.g. 1.36, pinned in tfvars because the add-on pins in addon_versions are built for one minor."
  type        = string

  validation {
    condition     = can(regex("^1\\.[0-9]+$", var.kubernetes_version))
    error_message = "kubernetes_version must be a minor version like 1.36, never a patch version."
  }
}

variable "addon_versions" {
  description = "The EKS add-on versions, keyed by add-on name, one for every add-on eks.tf installs. Pinned in tfvars: render_dial.py renders versions.yaml's eks-addons, because a version EKS publishes after review must not reach a deploy. Each is built for one Kubernetes minor, so a cluster on another minor sets all five to what `aws eks describe-addon-versions --kubernetes-version <minor>` lists, and the plan refuses a kube-proxy built for any minor but kubernetes_version."
  type        = map(string)
  nullable    = false

  validation {
    condition     = alltrue([for v in values(var.addon_versions) : can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+-eksbuild\\.[0-9]+$", v))])
    error_message = "Every addon_versions value must be an EKS add-on version such as v1.36.0-eksbuild.45, never a bare upstream version or latest."
  }
}

variable "node_pools" {
  description = "Managed node groups, keyed by pool name, from tfvars: the dial's own pools plus the ones the shape resolver derives. shape_ref indexes resolved_shapes, which supplies the instance types and the root volume type -- neither is ever written here."
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

  validation {
    condition     = length(var.node_pools) > 0
    error_message = "At least one node pool is needed -- an EKS cluster with no nodes runs nothing."
  }

  validation {
    condition     = alltrue([for p in var.node_pools : contains(["ON_DEMAND", "SPOT"], p.capacity_type)])
    error_message = "capacity_type must be ON_DEMAND or SPOT -- the only two values the EKS API takes."
  }

  validation {
    condition     = alltrue([for p in var.node_pools : p.min_size <= p.desired_size && p.desired_size <= p.max_size])
    error_message = "Each pool needs min_size <= desired_size <= max_size."
  }

  validation {
    condition     = alltrue([for p in var.node_pools : alltrue([for t in p.taints : contains(["NO_SCHEDULE", "NO_EXECUTE", "PREFER_NO_SCHEDULE"], t.effect)])])
    error_message = "A taint effect must be NO_SCHEDULE, NO_EXECUTE or PREFER_NO_SCHEDULE -- the EKS API spellings, not the Kubernetes ones."
  }

  validation {
    condition     = alltrue([for p in var.node_pools : contains(keys(var.resolved_shapes), p.shape_ref)])
    error_message = "Every node pool's shape_ref must have an entry in resolved_shapes."
  }

  validation {
    condition     = alltrue([for p in var.node_pools : try(var.resolved_shapes[p.shape_ref].root_volume != null, false)])
    error_message = "Every node pool's shape must carry a root_volume in resolved_shapes -- re-run scripts/resolve_sizing.py, which writes one for every shape with a root disk."
  }
}

variable "resolved_shapes" {
  description = "Auto-selected by the shape resolver (scripts/resolve_sizing.py) under each shape's generation policy in shapes/compute-shapes.yaml, keyed by shape_ref: the newest arm64 generation in the region, or the generation the shape pins. The node shapes are pinned to Graviton4 (generation 8) while aws-network-policy-agent #686 leaves pod traffic timing out on Graviton5 nodes, and compute-shapes.yaml names the unpin trigger beside each pin. instance_types is ordered best-first so EKS can fall back when the first is short of capacity, and a pinned shape holds one generation, so its list can be a single type. root_volume is the shape's root disk type and size from shapes/compute-shapes.yaml. instance_store_policy is the resolver's answer for a shape whose volume profile asks for its instance store: a node group of that shape gets the matching nodeadm localStorage strategy, the one its Karpenter pool's instanceStorePolicy sets."
  type = map(object({
    instance_types = list(string)
    arch           = string
    root_volume = optional(object({
      type     = string
      size_gib = number
    }))
    instance_store_policy = optional(string)
  }))

  validation {
    condition     = alltrue([for s in var.resolved_shapes : length(s.instance_types) > 0])
    error_message = "Every resolved shape needs at least one instance type."
  }

  // Karpenter's instanceStorePolicy takes RAID0 alone, so the node groups take the same.
  validation {
    condition     = alltrue([for s in var.resolved_shapes : s.instance_store_policy == null || s.instance_store_policy == "RAID0"])
    error_message = "instance_store_policy must be RAID0 or unset -- the one value a shape's Karpenter pool accepts, so both node paths assemble the NVMe alike."
  }

  validation {
    condition     = alltrue([for s in var.resolved_shapes : s.arch == "arm64"])
    error_message = "Cloud node pools are arm64. An x86 shape here means the resolver was asked for the wrong thing, or an on-prem shape reached a cloud root."
  }
}

variable "node_image" {
  description = "The EKS-optimised image every managed node group launches, from the node-image cascade (CONTRACT.md): the newest awslabs/amazon-eks-ami release that clears the release gates in versions.yaml's node-images block, auto-selected by scripts/resolve_sizing.py images, else tfvars. family is the EKS AMI type and version its dated release, so a node group's image moves only when a resolve and an apply move it. A null version leaves the release to EKS, ungated, which is why the aws root refuses one."
  type = object({
    family  = string
    version = optional(string)
  })
  nullable = false

  // The shapes are arm64-only, so the image family has to be one of EKS's arm64 types.
  validation {
    condition     = can(regex("_ARM_64(_|$)", var.node_image.family))
    error_message = "node_image.family must be an arm64 EKS AMI type, because every resolved shape is arm64. Got ${var.node_image.family}."
  }

  validation {
    condition     = var.node_image.version == null || (can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+-[0-9]{8}$", var.node_image.version)) && startswith(coalesce(var.node_image.version, "-"), "${var.kubernetes_version}."))
    error_message = "node_image.version must be an EKS release for Kubernetes ${var.kubernetes_version}, <patch>-<YYYYMMDD> such as ${var.kubernetes_version}.4-20261012, or null."
  }
}

variable "network" {
  description = "From tfvars (the dial's network block). nat = per-az puts one NAT gateway in each availability zone, so a zone failure and cross-zone data charges both stay contained. nat = single puts one in the first, which is cheaper. az_count is how many zones the VPC spans, 2-6. subnet_newbits is how many bits each subnet adds to provision.cidr: the VPC splits into 2^subnet_newbits equal blocks, private subnets from the first and public from the midpoint, and every pod takes an address from its node's private subnet under the VPC CNI."
  type = object({
    nat            = string
    az_count       = number
    subnet_newbits = number
  })

  validation {
    condition     = contains(["per-az", "single"], var.network.nat)
    error_message = "network.nat must be per-az or single."
  }

  validation {
    condition     = var.network.az_count >= 2 && var.network.az_count <= 6
    error_message = "network.az_count must be between 2 and 6 -- below 2 there is no redundancy to speak of, and no AWS region offers more than 6."
  }

  // The public half starts at the midpoint, so each half needs a block per zone.
  validation {
    condition     = var.network.subnet_newbits == floor(var.network.subnet_newbits) && pow(2, var.network.subnet_newbits - 1) >= var.network.az_count
    error_message = "network.subnet_newbits must be a whole number with 2^(subnet_newbits - 1) at least network.az_count, so the private and public halves each hold a subnet per zone."
  }
}

variable "endpoint" {
  description = "The private API endpoint is always on. public adds the public one, reachable only from allowed_cidrs."
  type = object({
    public        = bool
    allowed_cidrs = list(string)
  })

  validation {
    condition     = !var.endpoint.public || length(var.endpoint.allowed_cidrs) > 0
    error_message = "A public API endpoint with no allowed_cidrs is open to the internet. Name the addresses that may reach it."
  }

  validation {
    condition     = !contains(var.endpoint.allowed_cidrs, "0.0.0.0/0")
    error_message = "0.0.0.0/0 is not an allowlist. Name the addresses that may reach the API."
  }
}

variable "dns" {
  description = "private_zone is created and resolves inside the VPC only. There is no public_zone here -- the public zone and the two controller identities that write it belong to the edge module, which is what a caller turns off to render no public door at all."
  type = object({
    private_zone = string
  })

  validation {
    condition     = length(var.dns.private_zone) > 0
    error_message = "dns.private_zone is required -- internal services resolve through it."
  }
}

variable "telemetry" {
  description = <<-DESCRIPTION
    Where a CloudWatch-only touchpoint lands, and for how long, from tfvars (the
    dial's telemetry block). EKS delivers control-plane logs to CloudWatch and
    nowhere else -- there is no S3 or OTel export for them -- so the streams
    named in eks_log_types (audit by default, the one that matters for
    compliance) are always written to CloudWatch under both sinks. An empty
    eks_log_types creates no log group. sink = otel (DFE's monitoring goes to
    its own OTel feed and HyperDX everywhere else) keeps it at
    forced_retention_days, since this log group is an unavoidable exception
    rather than a chosen destination. sink = cloudwatch, the opt-in AWS-native
    path, keeps it at retention_days like every other CloudWatch touchpoint
    under that sink.
  DESCRIPTION

  type = object({
    sink                  = string
    retention_days        = number
    forced_retention_days = number
    eks_log_types         = list(string)
  })

  validation {
    condition     = contains(["otel", "cloudwatch"], var.telemetry.sink)
    error_message = "telemetry.sink must be otel or cloudwatch."
  }

  // The five streams the EKS API accepts. Each one enabled is CloudWatch ingestion billed per GB.
  validation {
    condition = alltrue([
      for t in var.telemetry.eks_log_types :
      contains(["api", "audit", "authenticator", "controllerManager", "scheduler"], t)
    ]) && length(distinct(var.telemetry.eks_log_types)) == length(var.telemetry.eks_log_types)
    error_message = "telemetry.eks_log_types must list each of api, audit, authenticator, controllerManager and scheduler at most once."
  }

  validation {
    condition     = var.telemetry.retention_days > 0
    error_message = "telemetry.retention_days must be positive."
  }

  validation {
    // CloudWatch Logs takes a fixed set of retention periods and rejects
    // anything else.
    condition = var.telemetry.sink != "cloudwatch" || contains(
      [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653],
      var.telemetry.retention_days
    )
    error_message = "telemetry.retention_days must be one of the retention periods CloudWatch Logs accepts when sink is cloudwatch."
  }

  // The audit log group is CloudWatch under either sink, so its retention is always one CloudWatch accepts.
  validation {
    condition = contains(
      [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653],
      var.telemetry.forced_retention_days
    )
    error_message = "telemetry.forced_retention_days must be one of the retention periods CloudWatch Logs accepts."
  }
}

variable "key_policy_grants" {
  description = "Extra statements the deployment's KMS key policy must carry, beyond the account-root delegation to IAM. A service principal (cloudtrail.amazonaws.com, delivery.logs.amazonaws.com) is not an IAM identity, so it cannot be granted through an aws_iam_role_policy the way the cluster role above is -- its access has to live in the key's own policy. This module is the key's ONE policy owner (kms.tf), so every caller that needs one hands its statement in here rather than writing a second aws_kms_key_policy against the same key, which would silently replace this one's."
  type = list(object({
    sid        = string
    principals = list(string)
    actions    = list(string)
    conditions = optional(list(object({
      test     = string
      variable = string
      values   = list(string)
    })), [])
  }))
  default = []
}

variable "toolbox_operator_role_arn" {
  description = "IAM role ARN granted a read-only (AmazonEKSViewPolicy) EKS access entry for the on-demand toolbox's operator identity. Empty (the default) creates no entry at all -- not every caller runs the toolbox, and the toolbox INSTANCE itself never gets one either way (eks.tf, 'Toolbox operator')."
  type        = string
  default     = ""
}

variable "clickhouse_object_store_namespace" {
  description = "Namespace the clickhouse-cluster chart deploys into (argocd/appsets/layer2-data.yaml pins it to \"clickhouse\"), for the S3 object-store Pod Identity association (object-store.tf)."
  type        = string
  default     = "clickhouse"
}

variable "clickhouse_object_store_service_account" {
  description = "Service account the ClickHouse server pods run as. Must match the chart's clickhouse.serviceAccount.name (default dfe-clickhouse, helm/charts/clickhouse-cluster/values.yaml) -- clickhouse.yaml and clickhouse-single.yaml both render that account onto their pods so the Pod Identity association binds only them, not the release namespace's default account every other workload there would otherwise share."
  type        = string
  default     = "dfe-clickhouse"
}

variable "tags" {
  description = "The governance tag set. Validated here and applied by the root's provider default_tags, so no resource in this module carries the map itself."
  type        = map(string)

  validation {
    condition = alltrue([
      for k in ["service-name", "service-namespace", "environment", "owner", "cost-center", "lifecycle", "iac-source"] :
      lookup(var.tags, k, "") != ""
    ])
    error_message = "tags must carry service-name, service-namespace, environment, owner, cost-center, lifecycle and iac-source, each non-empty."
  }
}

variable "permissions_boundary" {
  description = "IAM policy ARN set as the permissions boundary on every role this module creates, or null for none."
  type        = string
  default     = null
}

variable "iam_path" {
  description = "IAM path for every role and instance profile this module creates, or null for the default \"/\"."
  type        = string
  default     = null
}

variable "s3_bucket_prefix" {
  description = "Prepended to the name of every bucket this module creates. Empty by default; an account that scopes S3 writes to a name prefix sets it."
  type        = string
  default     = ""
}

variable "node_user_data" {
  description = "A shell script every managed node runs before kubelet starts, wrapped in the MIME part EKS merges ahead of its own NodeConfig: the root passes the registry-cache module's containerd mirror script. Empty (the default) adds none. A node group that gains its first launch template is replaced."
  type        = string
  default     = ""
  nullable    = false
}

variable "controller_tags" {
  description = "Tags the EKS add-ons put on what they create themselves -- the EBS CSI driver's volumes and the VPC CNI's pod network interfaces -- which the provider's default_tags never sees. Empty (the default) leaves both add-ons' configuration exactly as it was. The aws root passes a test run's id and expiry here."
  type        = map(string)
  default     = {}
}
