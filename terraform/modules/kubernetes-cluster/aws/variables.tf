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

  validation {
    condition     = can(cidrsubnet(var.provision.cidr, 4, 0))
    error_message = "provision.cidr must be a CIDR block with room for at least 16 subnets (a /20 or larger)."
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
  description = "EKS control-plane minor version, e.g. 1.36."
  type        = string

  validation {
    condition     = can(regex("^1\\.[0-9]+$", var.kubernetes_version))
    error_message = "kubernetes_version must be a minor version like 1.36, never a patch version."
  }
}

variable "node_pools" {
  description = "Managed node groups, keyed by pool name. shape_ref indexes resolved_shapes -- an instance type is never written here."
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
}

variable "resolved_shapes" {
  description = "The shape resolver's answer, keyed by shape_ref. instance_types is ordered best-first so EKS can fall back when the first is short of capacity."
  type = map(object({
    instance_types = list(string)
    arch           = string
  }))

  validation {
    condition     = alltrue([for s in var.resolved_shapes : length(s.instance_types) > 0])
    error_message = "Every resolved shape needs at least one instance type."
  }

  validation {
    condition     = alltrue([for s in var.resolved_shapes : s.arch == "arm64"])
    error_message = "Cloud node pools are arm64. An x86 shape here means the resolver was asked for the wrong thing, or an on-prem shape reached a cloud root."
  }
}

variable "network" {
  description = "nat = per-az puts one NAT gateway in each availability zone, so a zone failure and cross-zone data charges both stay contained. nat = single puts one in the first, which is cheaper and is the tyre-kick default."
  type = object({
    nat = string
  })

  validation {
    condition     = contains(["per-az", "single"], var.network.nat)
    error_message = "network.nat must be per-az or single."
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
  description = "private_zone is created and resolves inside the VPC only. public_zone is created when non-empty and its name servers are output for the parent zone's delegation."
  type = object({
    private_zone = string
    public_zone  = string
  })

  validation {
    condition     = length(var.dns.private_zone) > 0
    error_message = "dns.private_zone is required -- internal services resolve through it."
  }
}

variable "telemetry" {
  description = <<-DESCRIPTION
    Where a CloudWatch-only touchpoint lands, and for how long. EKS delivers
    control-plane logs to CloudWatch and nowhere else -- there is no S3 or OTel
    export for them -- so this module always enables the audit stream (the one
    that matters for compliance) and always writes it to CloudWatch under both
    sinks. sink = otel (the default, DFE's monitoring goes to its own OTel feed
    and HyperDX everywhere else) pins retention to the 1-day floor, since this
    log group is an unavoidable exception rather than a chosen destination.
    sink = cloudwatch, the opt-in AWS-native path, keeps it at retention_days
    like every other CloudWatch touchpoint under that sink.
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
    // anything else.
    condition = var.telemetry.sink != "cloudwatch" || contains(
      [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653],
      var.telemetry.retention_days
    )
    error_message = "telemetry.retention_days must be one of the retention periods CloudWatch Logs accepts when sink is cloudwatch."
  }
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
