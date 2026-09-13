// The DFE_* set is the hand-off to bootstrap.sh, read by bootstrap/bridge.py
// from `tofu output -json`. Same contract as the local root's, for a cloud.

output "DFE_ENV" {
  value = var.env
}

output "DFE_CLOUD" {
  value = local.cloud
}

output "DFE_REGION" {
  value = var.provision.region
}

output "DFE_DOMAIN" {
  description = "The private zone. Everything internal answers under it and resolves inside the VPC only."
  value       = var.dns.private_zone
}

output "DFE_PUBLIC_DOMAIN" {
  description = "The delegated public zone the exposed UIs answer on. Empty when the deployment has no public names."
  value       = var.dns.public_zone
}

output "DFE_PROFILE" {
  value = var.profile
}

output "DFE_REPO_URL" {
  value = var.repo_url
}

output "DFE_TARGET_REVISION" {
  value = var.target_revision
}

output "DFE_STORAGE_CLASS" {
  value = var.storage_class
}

output "DFE_NAMESPACE" {
  value = module.naming.k8s_namespace
}

output "DFE_CLICKHOUSE_HOST" {
  value = var.endpoints.clickhouse_host
}

output "DFE_KAFKA_PROVIDER" {
  description = "Who runs the brokers -- strimzi or redpanda in the cluster, msk/confluent-cloud/redpanda-cloud managed. The chart's kafka.provider takes it unchanged, and it is the last path segment of the credential's store key."
  value       = var.kafka.provider
}

output "DFE_KAFKA_BOOTSTRAP" {
  description = "What DFE connects to, in one spelling: the managed broker's bare host:port on msk, confluent-cloud or redpanda-cloud, and otherwise the in-cluster address the dial supplies -- empty on a brokerless profile."
  value       = local.managed_kafka.bootstrap != "" ? local.managed_kafka.bootstrap : var.endpoints.kafka_bootstrap
}

output "DFE_KAFKA_BOOTSTRAP_IAM" {
  description = "The IAM-authenticated endpoint, which the bootstrap Job alone connects to -- it is how the first Kafka ACL gets created on a cluster that grants nothing by default. Empty unless a managed broker offers one."
  value       = local.managed_kafka.bootstrap_iam
}

output "DFE_KAFKA_CREDENTIAL_REF" {
  description = "Where the broker's own copy of the SCRAM credential lives -- the secret MSK authenticates against, named rather than read. DFE's own copy is the kafka/<provider> entry under DFE_SECRETS_PREFIX, and both carry the same password."
  value       = local.managed_kafka.credential_ref
}

output "DFE_KAFKA_BOOTSTRAP_ROLE_ARN" {
  description = "The role the in-cluster bootstrap Job runs as, already associated with its service account. The chart renders the Job; this root minted what it authenticates with."
  value       = local.managed_kafka.bootstrap_role_arn
}

output "DFE_OTEL_ENDPOINT" {
  value = var.endpoints.otel_endpoint
}

output "DFE_TELEMETRY_SINK" {
  description = "otel or cloudwatch, echoed from the dial. bootstrap.sh reads this to decide whether the fetcher's AWS telemetry pre-config template renders at all -- it is otel-only, since the cloudwatch sink has nothing for the fetcher to pre-configure against."
  value       = var.telemetry.sink
}

output "DFE_KAFKA_BROKER_LOG_BUCKET" {
  description = "The S3 bucket MSK's broker logs land in under telemetry.sink = otel, for the fetcher's object-store source. Empty on every other provider and under the cloudwatch sink."
  value       = join("", module.kafka[*].broker_log_bucket)
}

output "DFE_CLOUDTRAIL_BUCKET" {
  description = "The S3 bucket CloudTrail delivers to, under both telemetry sinks. The fetcher's aws source reads it regardless of which sink is chosen."
  value       = aws_s3_bucket.cloudtrail.bucket
}

output "DFE_EKS_AUDIT_LOG_GROUP" {
  description = "The CloudWatch log group EKS's control-plane audit stream lands in -- unavoidable under either sink. No documented fetcher CloudWatch Logs source exists today, so this is named for when one does, not consumed by bootstrap.sh yet."
  value       = module.cluster.audit_log_group
}

output "DFE_WORKLOAD_IDENTITY_ANNOTATIONS" {
  description = "Empty on this cloud. EKS Pod Identity binds a role to a service account from OUTSIDE the cluster, so no service account carries an annotation -- unlike IRSA, Workload Identity Federation and Entra Workload ID, which all need one."
  value       = "{}"
}

output "DFE_SECRETS_BACKEND" {
  value = var.secrets.backend
}

output "DFE_SECRETS_REGION" {
  value = module.secrets.store_config.region
}

output "DFE_SECRETS_PREFIX" {
  description = "Path every ExternalSecret's remoteRef is relative to."
  value       = module.secrets.store_config.prefix
}

output "DFE_ESO_ROLE_ARN" {
  description = "The role external-secrets assumes. Already associated with its service account, so this is for the record rather than for wiring."
  value       = module.secrets.eso_role_arn
}

output "DFE_DNS_PROVIDER" {
  description = "external-dns provider name. Its own default is aws, and an uncredentialled install crash-loops against Route 53 -- so it is stated rather than defaulted."
  value       = "aws"
}

output "DFE_REGISTRY_HOST" {
  value = var.registry_host
}

output "DFE_REGISTRY_USER" {
  value = var.registry_user
}

output "DFE_REGISTRY_TOKEN" {
  value     = var.registry_token
  sensitive = true
}

// ---------------------------------------------------------------------------
// Operator-facing
// ---------------------------------------------------------------------------

output "cluster_name" {
  value = module.cluster.cluster_name
}

output "DFE_KUBE_CLUSTER_NAME" {
  description = "Bootstrap reads this into the Argo cluster secret's dfe.hyperi.io/cluster_name annotation."
  value       = module.cluster.cluster_name
}

output "DFE_KARPENTER_DISCOVERY_TAG" {
  description = "Bootstrap reads this into the Argo cluster secret's dfe.hyperi.io/karpenter_discovery_tag annotation, which layer2-platform.yaml carries into karpenter-pools' karpenter.cluster.discoveryTag."
  value       = module.cluster.karpenter.discovery_tag
}

output "DFE_KARPENTER_INSTANCE_PROFILE" {
  description = "Bootstrap reads this into the Argo cluster secret's dfe.hyperi.io/karpenter_instance_profile annotation, which layer2-platform.yaml carries into karpenter-pools' karpenter.cluster.instanceProfile."
  value       = module.cluster.karpenter.instance_profile
}

output "DFE_KARPENTER_KMS_KEY_ID" {
  description = "Bootstrap reads this into the Argo cluster secret's dfe.hyperi.io/karpenter_kms_key_id annotation, which layer2-platform.yaml carries into karpenter-pools' karpenter.cluster.kmsKeyId -- the same deployment CMK as kms_key_arn below."
  value       = module.cluster.kms_key_arn
}

output "cluster_endpoint" {
  value = module.cluster.cluster_endpoint
}

output "kubeconfig_command" {
  description = "What to run before anything else."
  value       = "aws eks update-kubeconfig --name ${module.cluster.cluster_name} --region ${var.provision.region}"
}

output "public_zone_name_servers" {
  description = "The NS set the parent zone must delegate to before any public name resolves. Empty when there is no public zone."
  value       = module.cluster.public_zone_name_servers
}

output "kms_key_arn" {
  description = "The deployment's customer-managed key -- cluster secrets, the secret store, and Kafka and block storage when they land."
  value       = module.cluster.kms_key_arn
}

output "network" {
  description = "VPC, CIDR, zones and subnets, for whatever attaches to them next."
  value       = module.cluster.network
}
