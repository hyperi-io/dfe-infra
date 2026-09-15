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

variable "vpc_id" {
  description = "The VPC the cluster sits in (kubernetes-cluster/aws's network.vpc_id). Not read by the IAM half of this module -- it is what a security group in front of the tunnel attaches to."
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

variable "tags" {
  description = "The governance tag set, applied to every taggable resource through the provider's default_tags. Merged here only where a resource takes a Name of its own."
  type        = map(string)
}
