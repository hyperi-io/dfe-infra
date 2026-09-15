// The edge module's AWS flavour: everything whose job is to decide whether
// traffic from outside the cluster may reach a DFE workload. The cluster module
// keeps the private zone, because nothing crosses that line.
//
// The whole module is gated by the root's `count` (terraform/environments/aws/
// main.tf), so nothing here carries an `enabled` input -- a disabled edge is a
// module instance that does not exist, not one full of zero-count resources.

variable "name" {
  description = "Name prefix for every resource this module creates -- the same deployment name the cluster module is given, so a role reads as this deployment's."
  type        = string
}

variable "env" {
  description = "Deployment environment, echoed into tags and descriptions."
  type        = string
}

variable "cluster_name" {
  description = "The EKS cluster every Pod Identity association here binds against (kubernetes-cluster/aws's cluster_name output). Pod Identity resolves a credential by cluster, namespace and service account, so an association naming the wrong cluster hands the controller nothing and the controller still reports Ready."
  type        = string
}

variable "network" {
  description = "Where the tunnel forwarder lands (kubernetes-cluster/aws's network output). cidr scopes its egress to the cluster's own nodes; azs and public_subnet_ids are parallel lists, so the zone the forwarder pins to selects the subnet it launches in. The forwarder holds an Elastic IP, which is only delivered where the subnet's route table sends 0.0.0.0/0 at an internet gateway, so it takes the PUBLIC list. Read only by the forwarder -- the IAM half of this module needs no network at all."
  type = object({
    vpc_id            = string
    cidr              = string
    azs               = list(string)
    public_subnet_ids = list(string)
  })
}

variable "node_security_group_id" {
  description = "The group every cluster node carries (kubernetes-cluster/aws's cluster_security_group_id). EKS attaches that one group to a managed node group's instances, because no launch template gives them one of their own, and its ingress admits its own members alone -- so a packet the forwarder DNATs at a nodePort is dropped at the node until this module names the forwarder's group on it. Read only on address.mode forwarder; byo adds no rule to it at all."
  type        = string
}

variable "kms_key_arn" {
  description = "The deployment's customer-managed key (kubernetes-cluster/aws's kms_key_arn output), encrypting the forwarder's root volume. This module NEVER writes to the key's own resource policy -- aws_kms_key_policy replaces the whole policy and the cluster module is its one owner."
  type        = string
}

variable "pod_identity_trust_policy_json" {
  description = "The trust policy an in-cluster workload identity assumes (kubernetes-cluster/aws's pod_identity_trust_policy_json output). Taken from the cluster module rather than rebuilt here, so this module mints a role without knowing how this cloud expresses cluster trust."
  type        = string
}

variable "private_zone_arn" {
  description = "The cluster module's private hosted zone (its private_zone_arn output). external-dns is ONE controller writing both zones, so its role lives here with the public zone and is granted the private one by ARN -- narrowing this to the public zone alone would leave every internal record unwritten."
  type        = string
}

variable "dns" {
  description = "public_zone is the delegated zone the exposed UIs answer on, created when non-empty. Empty means no public names, no public zone, and no DNS-01 identity for cert-manager to assume."
  type = object({
    public_zone = string
  })
}

variable "tunnel" {
  description = <<-DESCRIPTION
    The fleet tunnel's cloud-side address (the dial's edge.ingest.tunnel).

    address.mode is `byo` -- an address the deployer already has in front of
    culvert's NodePort -- or `forwarder`, which is this module's own Elastic IP
    on a small instance that DNATs to the cluster's nodes. The tier-2 opt-in is
    one instance and one static address, both billed hourly.

    address.instance_type is sized by BANDWIDTH: the forwarder moves every
    tunnel byte and does nothing else, and within a burstable family the
    baseline network bandwidth rises with the size. The default is the shape
    shapes/compute-shapes.yaml already names for a small always-on AWS box (use
    case `toolbox`: family t, arch arm64, generation newest, size small).

    address.zone pins the instance to one availability zone, because a hop to a
    node in another zone crosses a boundary billed per GB. Empty takes the
    network's first zone, and the public subnet in that zone is what the
    instance launches in.

    openvpn follows the culvert chart's own listeners list, which exposes
    WireGuard and OpenVPN over UDP by default -- false opens 51820 alone.

    source_ranges is the tunnel's allow-list, the same list the chart's
    exposure.loadBalancerSourceRanges carries. Empty is 0.0.0.0/0 by design: an
    edge fleet dialling in from anywhere is the normal case.

    node_ports must match the nodePort the culvert chart pins for each listener
    -- a DNAT aimed at a port nothing listens on is a tunnel that never
    connects, and Kubernetes would otherwise allocate a port tofu cannot know.
  DESCRIPTION

  type = object({
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
  })

  default = {}

  validation {
    condition     = contains(["byo", "forwarder"], var.tunnel.address.mode)
    error_message = "tunnel.address.mode must be byo (an address the deployer brings) or forwarder (this module's own Elastic IP)."
  }

  validation {
    condition     = can(regex("^[a-z][a-z0-9]*g[a-z]*\\.[a-z0-9-]+$", var.tunnel.address.instance_type))
    error_message = "tunnel.address.instance_type must name a Graviton (arm64) family -- the generation letter is followed by a literal 'g' (t4g, m7g, c8gn, ...) -- to match the AL2023 arm64 AMI this module resolves. Got ${var.tunnel.address.instance_type}."
  }

  validation {
    condition = alltrue([
      for port in [var.tunnel.node_ports.wireguard, var.tunnel.node_ports.openvpn] :
      port >= 30000 && port <= 32767
    ])
    error_message = "every tunnel.node_ports entry must sit in the Kubernetes node-port range 30000-32767 -- the API server refuses a Service asking for anything else."
  }
}

variable "tags" {
  description = "The governance tag set, applied to every taggable resource through the provider's default_tags. Merged here only where a resource takes a Name of its own."
  type        = map(string)
}
