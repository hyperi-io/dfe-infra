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

output "cluster_created_at" {
  description = "When EKS created the control plane, as the API reports it. The one creation stamp this deployment does not have to keep itself, and what dates an ephemeral deployment -- a local state file's mtime dates the last apply and a remote backend leaves none at all."
  value       = aws_eks_cluster.this.created_at
}

output "cluster_version" {
  description = "The version EKS is actually running, which can lead the requested minor after an AWS-side patch."
  value       = aws_eks_cluster.this.version
}

output "oidc_issuer" {
  description = "The cluster's OIDC issuer URL. Pod Identity does not use it; an external verifier of cluster-issued tokens does."
  value       = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

output "cluster_security_group_id" {
  description = "The security group EKS created for the control plane, which is what admits a caller to the Kubernetes API. Nodes and pods reach the API because they are already trusted by it; anything else in the VPC -- the toolbox instance, a bastion a caller brings -- has to be admitted by name, and this is the group that admits it."
  value       = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
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

output "private_zone_arn" {
  description = "The private zone as an IAM resource. external-dns's identity lives in the edge module with the public zone -- it is one controller writing both -- and this is how it is granted the write on the internal half without that module creating the zone."
  value       = aws_route53_zone.private.arn
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

output "clickhouse_object_store_bucket" {
  description = "The S3 bucket ClickHouse's cached-object storage model writes its bulk parts to (object-store.tf). Empty consumers never read this; the endpoint output below is the one the chart actually takes."
  value       = aws_s3_bucket.clickhouse_object_store.id
}

output "clickhouse_object_store_endpoint" {
  description = "The bucket URL in the form ClickHouse's S3 disk takes -- clickhouse.objectStore.endpoint, trailing slash included. Carried onto the cluster secret as DFE_CLICKHOUSE_OBJECT_STORE_ENDPOINT."
  value       = "https://${aws_s3_bucket.clickhouse_object_store.id}.s3.${var.provision.region}.amazonaws.com/dfe/"
}

output "clickhouse_object_store_role_arn" {
  description = "The Pod Identity role clickhouse_object_store_namespace/clickhouse_object_store_service_account authenticates as. Not consumed by the cluster secret today -- EKS Pod Identity resolves the credential by namespace + service account alone, the same reason the MSK bootstrap Job's role ARN is carried for completeness only (bootstrap.sh, DFE_KAFKA_BOOTSTRAP_ROLE_ARN)."
  value       = aws_iam_role.clickhouse_object_store.arn
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
