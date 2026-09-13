output "cluster_name" {
  description = "Feed to: aws eks update-kubeconfig --name <this> --region <region>"
  value       = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  description = "Kubernetes API server URL."
  value       = aws_eks_cluster.this.endpoint
}

output "cluster_ca" {
  description = "Base64 cluster CA, for the certificate-authority-data field of a kubeconfig."
  value       = aws_eks_cluster.this.certificate_authority[0].data
  sensitive   = true
}

output "cluster_version" {
  description = "The version EKS is actually running, which can lead the requested minor after an AWS-side patch."
  value       = aws_eks_cluster.this.version
}

output "oidc_issuer" {
  description = "The cluster's OIDC issuer URL. Pod Identity does not use it; an external verifier of cluster-issued tokens does."
  value       = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

output "network" {
  description = "What a sibling module attaches to -- managed Kafka on private connectivity needs the same subnets and the same CIDR."
  value = {
    vpc_id             = aws_vpc.this.id
    cidr               = aws_vpc.this.cidr_block
    azs                = local.azs
    private_subnet_ids = [for az in local.azs : aws_subnet.private[az].id]
    public_subnet_ids  = [for az in local.azs : aws_subnet.public[az].id]
  }
}

output "private_zone_id" {
  description = "Route 53 zone every internal name resolves in (external-dns --zone-id-filter)."
  value       = aws_route53_zone.private.zone_id
}

output "public_zone_id" {
  description = "Route 53 zone the public UI names resolve in. Empty when no public zone was asked for."
  value       = try(aws_route53_zone.public[0].zone_id, "")
}

output "public_zone_name_servers" {
  description = "The NS set the PARENT zone has to delegate to before any public name resolves. Empty when there is no public zone."
  value       = try(aws_route53_zone.public[0].name_servers, [])
}

output "kms_key_arn" {
  description = "The deployment's customer-managed key. Encrypts cluster secrets now; Kafka and block storage take the same one."
  value       = aws_kms_key.this.arn
}

output "pod_identity_trust_policy_json" {
  description = "The trust policy a further in-cluster workload identity assumes, so a sibling module mints a role without knowing how this cloud expresses cluster trust."
  value       = data.aws_iam_policy_document.pod_identity_trust.json
}

output "node_role_arn" {
  description = "The node instance role, for a caller that has to grant the nodes themselves something extra."
  value       = aws_iam_role.nodes.arn
}

output "audit_log_group" {
  description = "The CloudWatch log group EKS's control-plane audit stream lands in -- the one CloudWatch touchpoint this module cannot avoid, whatever telemetry.sink says. A caller that documents or scrapes it (a fetcher CloudWatch Logs source, for instance) reads this rather than reconstructing the naming convention."
  value       = aws_cloudwatch_log_group.cluster.name
}

output "karpenter" {
  description = "What the karpenter chart and the karpenter-pools chart have to be told: the queue to watch, the profile a node launches with, and the tag its subnets and security groups are found by. AWS-only -- a body on another cloud names its own provisioner here or nothing at all."
  value = {
    interruption_queue  = aws_sqs_queue.karpenter.name
    instance_profile    = aws_iam_instance_profile.karpenter_node.name
    discovery_tag       = var.name
    controller_role_arn = aws_iam_role.karpenter.arn
    node_role_arn       = aws_iam_role.karpenter_node.arn
  }
}
