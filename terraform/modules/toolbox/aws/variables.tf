// This module is ALWAYS instantiated by the aws root (no `count` on the
// module block) -- `enabled` gates every toggleable resource INSIDE it via a
// per-resource `count`. The one exception is the session-log S3 bucket, which
// carries no such gate at all and exists whenever the module does: it is the
// deployment's record of human access, and `bastion down` must not erase the
// evidence of the session it is closing out. See CONTRACT.md.

variable "name" {
  description = "Name prefix for every resource this module creates -- the tf-naming canonical_name for the toolbox component."
  type        = string
}

variable "env" {
  description = "Deployment environment, echoed into tags and document/bucket descriptions."
  type        = string
}

variable "enabled" {
  description = "Whether the toolbox INSTANCE exists. false (the `bastion down` state) tears down the instance, its security group, its instance profile and both kinds of Session document -- never merely stops the instance. The session-log bucket is NOT gated by this: it outlives every up/down cycle so a session's log is not deleted by the same call that closes the session."
  type        = bool
}

variable "network" {
  description = "Where the instance lands. cidr scopes the target-specific egress rules to the VPC itself, since every named target lives inside it; vpc_id and private_subnet_ids are the cluster module's own network output, unchanged."
  type = object({
    vpc_id             = string
    cidr               = string
    private_subnet_ids = list(string)
  })
}

variable "instance_type" {
  description = "EC2 instance type -- toolbox.aws.instance_type in the dial, t4g.small by default. Validated as a Graviton (arm64) family to match the AL2023 arm64 AMI this module always launches and shapes/compute-shapes.yaml's toolbox use case (family t, arch arm64)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9]*g[a-z]*\\.[a-z0-9-]+$", var.instance_type))
    error_message = "instance_type must name a Graviton (arm64) family -- the generation letter is followed by a literal 'g' (t4g, m7g, c8gn, ...) -- to match the AL2023 arm64 AMI this module resolves. Got ${var.instance_type}."
  }
}

variable "ttl_minutes" {
  description = "Minutes with no active SSM session before the on-instance timer shuts the box down (instance_initiated_shutdown_behavior = terminate, so the shutdown IS the teardown). Bounded here as well as in dfe-ops, because a TTL enforced only on the operator's laptop is not a bound at all -- see CONTRACT.md #9."
  type        = number

  validation {
    condition     = var.ttl_minutes >= 15 && var.ttl_minutes <= 480
    error_message = "ttl_minutes must be between 15 (below which a slow debugging pause looks idle) and 480 (8h -- above it this is a standing bastion by another name)."
  }
}

// The versions every install step in templates/user_data.sh.tftpl pins by
// name, never a literal in this module or the template. render_dial.py
// assembles this map from versions.yaml's toolbox: stage (the SAME stage
// docker/dfe-toolbox's Dockerfiles pin from, so the EC2 instance and the
// container image never drift apart) plus the services: stage for the two
// keys that track a server version instead of having one of their own --
// see CONTRACT.md for the exact keys and where each one comes from.
variable "tool_versions" {
  description = "kubectl, helm, argocd-cli, tofu, yq, aws-cli, aws-session-manager-plugin, clickhouse-client, psql -- every key required and non-empty. jq, kcat and openssl are NOT here: they carry no upstream release cadence worth tracking (versions.yaml's own toolbox: stage comment) and are installed unpinned, matching docker/dfe-toolbox/base/Dockerfile's identical choice."
  type        = map(string)

  // Only enforced when enabled is true: this module is ALWAYS instantiated
  // (variables.tf's header comment), so the rest state (enabled = false, no
  // dial has turned the toolbox on) must not fail validation just because
  // versions.yaml carries no toolbox stage yet -- see CONTRACT.md.
  validation {
    condition = !var.enabled || alltrue([
      for k in [
        "kubectl", "helm", "argocd-cli", "tofu", "yq", "aws-cli",
        "aws-session-manager-plugin", "clickhouse-client", "psql",
      ] : can(var.tool_versions[k]) && length(var.tool_versions[k]) > 0
    ])
    error_message = "tool_versions must carry a non-empty entry for kubectl, helm, argocd-cli, tofu, yq, aws-cli, aws-session-manager-plugin, clickhouse-client and psql whenever enabled is true -- render_dial.py assembles this from versions.yaml, so a missing key names a stanza that has not landed there yet."
  }
}

variable "session" {
  description = "Session Manager preferences for the SHELL document only (toolbox.session in the dial). A port-forward document carries no such preferences -- there is no shell to time out or log -- see CONTRACT.md #3/#6."
  type = object({
    idle_timeout_minutes = number
    max_duration_minutes = number
  })

  validation {
    condition     = var.session.idle_timeout_minutes >= 1 && var.session.idle_timeout_minutes <= 60
    error_message = "session.idle_timeout_minutes must be between 1 and 60 -- Session Manager's own ceiling."
  }

  validation {
    condition     = var.session.max_duration_minutes >= 1 && var.session.max_duration_minutes <= 1440
    error_message = "session.max_duration_minutes must be between 1 and 1440 -- Session Manager's own ceiling."
  }
}

variable "session_log_retention_days" {
  description = "S3 lifecycle expiry on the session-log bucket. Deliberately its OWN field, never telemetry.retention_days: a session log is evidence of human access to a customer's private data plane, not service telemetry, and Q48's 2-7 day telemetry window is far too short for it."
  type        = number
  default     = 90

  validation {
    condition     = var.session_log_retention_days >= 1
    error_message = "session_log_retention_days must be at least 1."
  }
}

variable "kms_key_arn" {
  description = "The deployment's customer-managed key (kubernetes-cluster/aws's kms_key_arn output). Used for the session-log bucket's SSE-KMS and the root volume's encryption. This module NEVER writes to the key's own resource policy: aws_kms_key_policy replaces the whole policy and the cluster module is its one owner (kms.tf). The key's account-root delegation statement is what makes a plain IAM role policy on the instance role sufficient for the bucket grant -- no key_policy_grants entry is needed either, unlike the CloudTrail/MSK-broker-log grants, which exist only because THOSE callers are AWS service principals with no IAM identity of their own; this module's caller is the instance's own IAM role."
  type        = string
}

variable "targets" {
  description = "Named forward targets -- host and port fixed at PLAN time, never a caller-supplied value. One customer-managed Session document per entry (sessionType Port, no host/portNumber parameter in the document body at all), so a forward session can only ever reach what this map names. Computed by the aws root from the cluster/kafka modules' own outputs (CONTRACT.md), never invented here."
  type = map(object({
    host = string
    port = number
  }))
  default = {}
}

variable "eks_cluster_security_group_id" {
  description = "The EKS control plane's own security group (kubernetes-cluster/aws's cluster_security_group_id). This module adds ONE ingress rule to it, 443 from the toolbox's own group, because nothing else admits the instance to the Kubernetes API: nodes and pods are trusted by that group already, and a brand-new group is not. That group is SHARED with the nodes -- karpenter.tf tags it for node discovery -- so the grant reaches TCP/443 on every Karpenter node too, which is a residual only while nothing hostNetwork binds 443 there. The rule is gated on `enabled` like everything else here, so `bastion down` takes the grant away with the instance. Empty adds no rule at all, for a caller with no cluster to reach."
  type        = string
  default     = ""
}

variable "force_destroy_session_logs" {
  description = "Whether the session-log bucket may be destroyed while it still holds objects. Follows the ROOT's tags.lifecycle the same way cloudtrail.tf's bucket does (main.tf's local.ephemeral) -- an ephemeral tyre-kick deployment is rebuilt under the same name and needs the fast teardown; a persistent one keeps the vendor default so destroying real session evidence needs a deliberate confirmation."
  type        = bool
}

variable "tags" {
  description = "The governance tag set. Merged with dfe.hyperi.io/component = toolbox on every resource this module tags -- the key every IAM policy example in CONTRACT.md scopes ssm:StartSession against, so an unconditioned grant elsewhere in the account cannot reach this instance and this instance's own tag cannot be renamed without also being the thing every relevant condition matches on."
  type        = map(string)
}
