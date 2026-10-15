// An allowed and a refused combination for every cloud offering.yaml covers,
// and the facts a root reads back. Hosts use the reserved example label and
// RFC 2606 names only: this repository is public.

variables {
  cloud          = "aws"
  kafka_provider = "strimzi"
}

// --- aws ---------------------------------------------------------------------

run "aws_offers_msk_with_its_scram_and_iam_ports" {
  command = plan

  variables {
    kafka_provider = "msk"
  }

  assert {
    condition     = output.kafka.managed && output.kafka.vendor_cloud == "aws"
    error_message = "msk is the managed Kafka of an aws deployment"
  }

  assert {
    condition     = output.kafka.client_ports == tolist([9096]) && output.kafka.bootstrap_ports == tolist([9098])
    error_message = "every app dials SCRAM on 9096 and only the bootstrap Job dials IAM on 9098"
  }
}

run "aws_builds_confluent_with_its_own_cloud_token" {
  command = plan

  variables {
    kafka_provider = "confluent-cloud"
    kafka_tier     = "freight"
  }

  assert {
    condition     = output.kafka.vendor_cloud == "AWS" && output.kafka.client_ports == tolist([9092])
    error_message = "Confluent takes AWS in capitals and serves Kafka on 9092"
  }
}

run "aws_builds_redpanda_serverless_on_its_seed_and_broker_ports" {
  command = plan

  variables {
    kafka_provider = "redpanda-cloud"
  }

  assert {
    condition     = output.kafka.vendor_cloud == "aws" && output.kafka.client_ports == tolist([9092, 9093, 9094, 9095])
    error_message = "Redpanda Serverless serves its seed on 9092 and its brokers on 9093 to 9095"
  }
}

run "aws_accepts_an_aws_clickhouse_cloud_service_public_or_private" {
  command = plan

  variables {
    clickhouse_mode = "external"
    clickhouse_host = "abc123.us-west-2.vpce.aws.clickhouse.cloud"
  }

  assert {
    condition     = output.clickhouse.service == "clickhouse-cloud" && output.clickhouse.vendor_cloud == "aws"
    error_message = "a private endpoint host still names its cloud in the label before the vendor's domain"
  }

  assert {
    condition     = output.clickhouse.client_ports == tolist([8443, 9440])
    error_message = "ClickHouse Cloud serves HTTPS on 8443 and the native protocol over TLS on 9440"
  }
}

run "aws_refuses_a_clickhouse_cloud_service_on_gcp" {
  command = plan

  variables {
    clickhouse_mode = "external"
    clickhouse_host = "abc123.us-central1.gcp.clickhouse.cloud"
  }

  expect_failures = [output.clickhouse]
}

run "aws_refuses_a_byoc_clickhouse_on_azure" {
  command = plan

  variables {
    clickhouse_mode = "external"
    clickhouse_host = "abc123.def456.westus3.azure.clickhouse-byoc.com"
  }

  expect_failures = [output.clickhouse]
}

// --- gcp ---------------------------------------------------------------------

run "gcp_offers_confluent_enterprise_as_gcp" {
  command = plan

  variables {
    cloud          = "gcp"
    kafka_provider = "confluent-cloud"
    kafka_tier     = "enterprise"
  }

  assert {
    condition     = output.kafka.vendor_cloud == "GCP"
    error_message = "a gcp deployment's Confluent cluster is Confluent's GCP instance"
  }

  assert {
    condition     = output.offered.kafka == tolist(["confluent-cloud", "redpanda-cloud"])
    error_message = "gcp offers Confluent Cloud and Redpanda Cloud, and no MSK"
  }
}

run "gcp_accepts_a_gcp_clickhouse_cloud_service" {
  command = plan

  variables {
    cloud           = "gcp"
    clickhouse_mode = "external"
    clickhouse_host = "abc123.us-central1.p.gcp.clickhouse.cloud"
  }

  assert {
    condition     = output.clickhouse.vendor_cloud == "gcp"
    error_message = "a Private Service Connect host names gcp before the vendor's domain"
  }
}

run "gcp_refuses_msk" {
  command = plan

  variables {
    cloud          = "gcp"
    kafka_provider = "msk"
  }

  expect_failures = [var.kafka_provider]
}

run "gcp_refuses_confluent_freight" {
  command = plan

  variables {
    cloud          = "gcp"
    kafka_provider = "confluent-cloud"
    kafka_tier     = "freight"
  }

  expect_failures = [var.kafka_tier]
}

run "gcp_refuses_a_clickhouse_cloud_service_on_aws" {
  command = plan

  variables {
    cloud           = "gcp"
    clickhouse_mode = "external"
    clickhouse_host = "abc123.us-west-2.aws.clickhouse.cloud"
  }

  expect_failures = [output.clickhouse]
}

// --- azure -------------------------------------------------------------------

run "azure_offers_confluent_basic_as_azure" {
  command = plan

  variables {
    cloud          = "azure"
    kafka_provider = "confluent-cloud"
    kafka_tier     = "basic"
  }

  assert {
    condition     = output.kafka.vendor_cloud == "AZURE" && output.offered.kafka == tolist(["confluent-cloud"])
    error_message = "azure offers Confluent Cloud alone, under Confluent's AZURE token"
  }
}

run "azure_accepts_an_azure_clickhouse_cloud_service" {
  command = plan

  variables {
    cloud           = "azure"
    clickhouse_mode = "external"
    clickhouse_host = "abc123.westus3.privatelink.azure.clickhouse.cloud"
  }

  assert {
    condition     = output.clickhouse.vendor_cloud == "azure"
    error_message = "an Azure Private Link host names azure before the vendor's domain"
  }
}

run "azure_refuses_redpanda_cloud" {
  command = plan

  variables {
    cloud          = "azure"
    kafka_provider = "redpanda-cloud"
  }

  expect_failures = [var.kafka_provider]
}

run "azure_refuses_msk" {
  command = plan

  variables {
    cloud          = "azure"
    kafka_provider = "msk"
  }

  expect_failures = [var.kafka_provider]
}

// --- what no cloud has to offer ----------------------------------------------

run "a_kafka_in_the_cluster_runs_on_every_cloud" {
  command = plan

  variables {
    cloud          = "azure"
    kafka_provider = "redpanda"
  }

  assert {
    condition     = !output.kafka.managed && output.kafka.vendor_cloud == null && length(output.kafka.client_ports) == 0
    error_message = "redpanda in the cluster involves no vendor and opens no managed-backend port"
  }
}

run "a_clickhouse_the_deployer_runs_is_not_a_vendor_service" {
  command = plan

  variables {
    cloud           = "gcp"
    clickhouse_mode = "external"
    clickhouse_host = "ch.example.com"
  }

  assert {
    condition     = output.clickhouse.service == "" && length(output.clickhouse.client_ports) == 0
    error_message = "a server the deployer runs is not checked against the offering"
  }
}

run "an_in_cluster_clickhouse_reads_no_host" {
  command = plan

  variables {
    clickhouse_host = "abc123.us-central1.gcp.clickhouse.cloud"
  }

  assert {
    condition     = output.clickhouse.service == ""
    error_message = "the host is the vendor's only on clickhouse.mode external"
  }
}

run "a_cloud_with_no_list_is_refused" {
  command = plan

  variables {
    cloud = "on-prem"
  }

  expect_failures = [var.cloud]
}
