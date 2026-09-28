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

output "DFE_TUNNEL_ADDRESS" {
  description = "The Elastic IP the fleet tunnel answers on. Bootstrap reads this into the Argo cluster secret's dfe.hyperi.io/tunnel_address annotation, which is what external-dns publishes vpn.serverCN at. Empty unless edge.tunnel.address.mode is forwarder -- on byo the deployer already holds the address and names it themselves."
  value       = join("", module.edge[*].tunnel_address)
}

output "DFE_TUNNEL_ZONE" {
  description = "The availability zone the tunnel forwarder is pinned to. Bootstrap reads this into the Argo cluster secret's dfe.hyperi.io/tunnel_zone annotation, which the edge flavour overlay turns into culvert's own zone nodeSelector -- a hop to a node in another zone crosses a boundary billed per GB. Empty unless the forwarder exists."
  value       = join("", module.edge[*].tunnel_zone)
}

output "DFE_TOOLBOX_ADMIN_CIDR" {
  description = "The toolbox instance's OWN address as a /32, which is the range culvert's admin exception is asked to admit -- the one shape it matches is a source arriving off the pod's ethernet side, never a peer. Bootstrap reads it into the Argo cluster secret's dfe.hyperi.io/toolbox_admin_cidr annotation and layer2-edge.yaml turns that into peers.classes.admin.adminCIDRs, while `dfe-ops bastion up` rewrites the same annotation each cycle because the instance is rebuilt and its address moves. Empty whenever no instance exists or the dial did not ask for the reach-back: an empty value drops the annotation, and culvert then renders neither the env key nor the NetworkPolicy ingress rule. It reads the toolbox module rather than the cluster's subnet list, which is also what puts it inside a `-target=module.toolbox` apply's own dependency set."
  value       = var.edge.enabled && var.edge.tunnel.admin_peer.enabled ? module.toolbox.admin_cidr : ""
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

output "DFE_VPC_ID" {
  description = "The VPC the cluster sits in, for the load balancer controller that must not learn it from instance metadata."
  value       = module.cluster.network.vpc_id
}

output "DFE_CLICKHOUSE_OBJECT_STORE_ENDPOINT" {
  description = "Bootstrap reads this into the Argo cluster secret's dfe.hyperi.io/clickhouse_object_store_endpoint annotation, which layer2-data.yaml carries into the clickhouse-cluster chart's clickhouse.objectStore.endpoint -- the fact that activates cached-object storage on this cloud (docs/deployment/storage.md)."
  value       = module.cluster.clickhouse_object_store_endpoint
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

output "cluster_created_at" {
  description = "When EKS created the control plane. `dfe-ops` dates an ephemeral deployment from this, because no file in the tree does: a local state file's mtime dates the last apply, and an S3 backend leaves none here at all."
  value       = module.cluster.cluster_created_at
}

output "kubeconfig_command" {
  description = "What to run before anything else."
  value       = "aws eks update-kubeconfig --name ${module.cluster.cluster_name} --region ${var.provision.region}"
}

output "public_zone_name_servers" {
  description = "The NS set the parent zone must delegate to before any public name resolves. Empty when there is no public zone, and empty when the edge module is off -- nothing then creates one."
  value       = flatten(module.edge[*].public_zone_name_servers)
}

output "public_zone_id" {
  description = "Route 53 zone the public names resolve in. Empty when there is no public zone or the edge module is off."
  value       = join("", module.edge[*].public_zone_id)
}

output "kms_key_arn" {
  description = "The deployment's customer-managed key -- cluster secrets, the secret store, and Kafka and block storage when they land."
  value       = module.cluster.kms_key_arn
}

output "network" {
  description = "VPC, CIDR, zones and subnets, for whatever attaches to them next."
  value       = module.cluster.network
}

// ---------------------------------------------------------------------------
// Toolbox -- read by `dfe-ops bastion {up,shell,forward,down,status}`.
// ---------------------------------------------------------------------------

output "toolbox_instance_id" {
  description = "Empty when the toolbox is not enabled -- dfe-ops bastion status/down reads this to tell 'never brought up' from 'torn down'."
  value       = module.toolbox.instance_id
}

output "toolbox_ssm_session_document" {
  description = "The SHELL Session document dfe-ops bastion shell invokes. Empty when not enabled."
  value       = module.toolbox.ssm_session_document
}

output "toolbox_targets" {
  description = "Named forward targets plus each one's Session document -- dfe-ops bastion forward <name> reads this rather than knowing any host or port itself."
  value       = module.toolbox.targets
}

output "toolbox_session_log_bucket" {
  description = "Where every shell session's transcript lands. Persists across up/down cycles."
  value       = module.toolbox.session_log_bucket
}
