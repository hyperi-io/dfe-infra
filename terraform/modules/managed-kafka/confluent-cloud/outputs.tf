// No zookeeper output: Confluent Cloud runs Kora and has none to describe.

output "bootstrap" {
  description = "The SASL_SSL bootstrap string DFE connects with, normalised to bare host:port. Confluent returns it with a SASL_SSL:// prefix, which is stripped here so every DFE reader downstream sees one spelling."
  value       = replace(confluent_kafka_cluster.this.bootstrap_endpoint, "/^[a-zA-Z0-9_+.-]+:\\/\\//", "")
}

output "bootstrap_port" {
  description = "The Kafka client port, 9092 -- a literal known at plan time, unlike the bootstrap host, so a caller can key a for_each on the target before the cluster exists."
  value       = 9092
}

output "bootstrap_iam" {
  description = "Empty. Confluent Cloud has no IAM-authenticated endpoint -- the contract carries this for MSK, where the in-cluster bootstrap Job needs one."
  value       = ""
}

output "auth_type" {
  description = "What the chart's external.auth.type takes unchanged. Confluent is SASL PLAIN over TLS on every tier and does NOT do SCRAM -- the API key is the username and the secret is the password."
  value       = "plain"
}

output "credential_ref" {
  description = "The Kafka API key's ID -- a reference, never a value. The secret behind it stays in state and in the secrets store, and is not handed out here."
  value       = confluent_api_key.dfe.id
}

output "network_attachment" {
  description = "The cloud side of the private handshake: the security group carried by the PrivateLink endpoint on Enterprise or by the Private Network Interfaces on Freight, and the subnets they live in. Empty on public connectivity, where there is nothing to route to."
  value = {
    security_group_id = local.private ? aws_security_group.private[0].id : ""
    subnet_ids        = local.private ? var.network.private_subnet_ids : []
  }
}

output "cluster_arn" {
  description = "The cluster's Confluent Resource Name, which is Confluent's equivalent of an ARN and what a role binding is written against."
  value       = confluent_kafka_cluster.this.rbac_crn
}

output "cluster_id" {
  description = "The vendor's handle on the cluster, for the alarms, the console link and the follow-on resources a caller adds outside this module."
  value       = confluent_kafka_cluster.this.id
}

output "bootstrap_role_arn" {
  description = "Empty. There is no in-cluster bootstrap Job on this path -- the manager service account writes the ACLs and topics from tofu, so no workload identity is minted for one."
  value       = ""
}
