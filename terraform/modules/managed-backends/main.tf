// The managed-backend offering. No resources: the validations in variables.tf
// are the enforcement, failing the plan of any root that calls this module, and
// the outputs are what that root hands the bodies it builds and the egress it
// opens. offering.yaml is the data; this file only reads it.

locals {
  offering = yamldecode(file("${path.module}/offering.yaml"))

  // Kafka that runs inside the deployment's own cluster, which every cloud can
  // carry because no vendor is involved.
  in_cluster_kafka = ["strimzi", "redpanda"]

  // try() throughout, because a cloud the file does not cover is refused by
  // var.cloud's own validation and must not also fail every lookup below.
  offered_kafka      = try(local.offering.clouds[var.cloud].kafka, {})
  offered_clickhouse = try(local.offering.clouds[var.cloud].clickhouse, {})

  kafka_managed = !contains(local.in_cluster_kafka, var.kafka_provider)
  kafka_offered = !local.kafka_managed || contains(keys(local.offered_kafka), var.kafka_provider)
  kafka_tiers   = try(local.offered_kafka[var.kafka_provider].tiers, [])

  // A ClickHouse Cloud host carries its cloud as the label before the vendor's
  // domain: <id>.<region>.<cloud>.clickhouse.cloud, or
  // <id>.<region>.vpce.aws.clickhouse.cloud on a private endpoint. Null for any
  // other host, which is a server the deployer runs and no vendor offers.
  clickhouse_domains = local.offering.backends["clickhouse-cloud"].host_domains
  clickhouse_host    = lower(trimsuffix(var.clickhouse_host, "."))
  clickhouse_cloud_label = one(compact([
    for domain in local.clickhouse_domains :
    try(regex("\\.([a-z0-9-]+)\\.${replace(domain, ".", "\\.")}$", local.clickhouse_host)[0], "")
  ]))
  clickhouse_cloud = var.clickhouse_mode == "external" && local.clickhouse_cloud_label != null

  // The cloud ClickHouse Cloud is offered on for this deployment, or null where
  // it is not offered at all.
  clickhouse_offered_cloud = try(local.offered_clickhouse["clickhouse-cloud"].vendor_cloud, null)

  kafka_backend      = try(local.offering.backends[var.kafka_provider], null)
  clickhouse_backend = local.offering.backends["clickhouse-cloud"]
}
