output "offered" {
  description = "What this cloud offers, by service: the managed Kafka and ClickHouse backend names from offering.yaml."
  value = {
    kafka      = sort(keys(local.offered_kafka))
    clickhouse = sort(keys(local.offered_clickhouse))
  }
}

output "kafka" {
  description = "The selected Kafka. managed is false for strimzi and redpanda. vendor_cloud is the token the vendor's API takes for the deployment's cloud, which the confluent-cloud and redpanda-cloud bodies are built with. client_ports are what every DFE client dials and bootstrap_ports what only the bootstrap Job dials; both are empty for a Kafka in the cluster."
  value = {
    provider        = var.kafka_provider
    managed         = local.kafka_managed
    vendor_cloud    = local.kafka_managed ? try(local.offered_kafka[var.kafka_provider].vendor_cloud, null) : null
    client_ports    = local.kafka_managed ? try([for port in local.kafka_backend.client_ports : tonumber(port)], []) : []
    bootstrap_ports = local.kafka_managed ? try([for port in local.kafka_backend.bootstrap_ports : tonumber(port)], []) : []
  }
}

output "clickhouse" {
  description = "The selected ClickHouse. service is clickhouse-cloud when the external host is a ClickHouse Cloud service, and empty for an in-cluster server or one the deployer runs. client_ports are the ports a DFE client dials on the vendor's service, empty when service is."
  value = {
    service      = local.clickhouse_cloud ? "clickhouse-cloud" : ""
    vendor_cloud = local.clickhouse_cloud ? local.clickhouse_cloud_label : null
    client_ports = local.clickhouse_cloud ? [for port in local.clickhouse_backend.client_ports : tonumber(port)] : []
  }

  precondition {
    condition     = !local.clickhouse_cloud || local.clickhouse_cloud_label == local.clickhouse_offered_cloud
    error_message = "clickhouse.host is a ClickHouse Cloud service on ${coalesce(local.clickhouse_cloud_label, "-")}, and this deployment runs on ${var.cloud}. A managed backend is offered only as its instance on the deployment's own cloud, because one on another cloud is reached over the internet and every byte to it is billed as internet egress. ${local.clickhouse_offered_cloud != null ? "Create the service on ${var.cloud} (a host under .${var.cloud}.clickhouse.cloud)" : "ClickHouse Cloud is not offered on ${var.cloud}"}, or set clickhouse.mode to in_cluster."
  }
}
