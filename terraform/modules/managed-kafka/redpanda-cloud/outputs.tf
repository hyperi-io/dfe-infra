// No zookeeper output: Redpanda has no ZooKeeper to describe.

output "bootstrap" {
  description = "The SASL/SCRAM bootstrap string DFE connects with, normalised to bare host:port pairs. Redpanda returns a LIST, and a private cluster returns a different list from a public one, so both are collapsed to the one spelling every DFE reader downstream expects."
  value       = local.bootstrap
}

output "bootstrap_iam" {
  description = "Empty. Redpanda Cloud has no IAM-authenticated endpoint -- the contract carries this for MSK, where the in-cluster bootstrap Job needs one."
  value       = ""
}

output "auth_type" {
  description = "What the chart's external.auth.type takes unchanged. Redpanda is SCRAM-SHA-512, the same mechanism the on-prem cluster uses."
  value       = "scram"
}

output "credential_ref" {
  description = "The Redpanda user's own ID -- a reference, never a value. The password reached Redpanda as a write-only argument and is not in state to hand out."
  value       = redpanda_user.this.id
}

output "network_attachment" {
  description = "The cloud side of the private handshake: the security group the PrivateLink endpoint carries and the subnets it lives in. Empty on public connectivity, where there is no endpoint to route to."
  value = {
    security_group_id = local.private ? aws_security_group.endpoint[0].id : ""
    subnet_ids        = local.private ? var.network.private_subnet_ids : []
  }
}

output "cluster_arn" {
  description = "Empty. Redpanda Cloud issues no ARN or CRN for a cluster; cluster_id below is the handle a caller uses."
  value       = ""
}

output "cluster_id" {
  description = "The vendor's handle on the cluster, for the alarms, the console link and the follow-on resources a caller adds outside this module."
  value       = redpanda_serverless_cluster.this.id
}

output "bootstrap_role_arn" {
  description = "Empty. There is no in-cluster bootstrap Job on this path -- the vendor provider writes the ACLs and topics itself, so no identity is minted for one."
  value       = ""
}
