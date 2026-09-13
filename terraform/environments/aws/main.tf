// The aws root is thin by design: one provider, one backend, a rendered tfvars
// file and the module calls. Everything that creates a resource lives in a
// capability module, so gcp and azure are new roots over new bodies rather than
// a fork of this file.

data "aws_caller_identity" "current" {}

locals {
  project = "dfe"
  cloud   = "aws"

  // Deletion protection follows the dial's tags.lifecycle, not a fixed answer:
  // ephemeral (tyre-kick, provision-test-destroy) deployments are torn down and
  // rebuilt under the same names, so they get the fastest teardown each service
  // allows; anything else -- persistent today, and any future value -- keeps
  // the vendor's own default so destroying a real deployment needs a
  // deliberate confirmation. (Named "lifecycle" is a reserved OpenTofu
  // variable identifier, so this reads tags.lifecycle rather than a var of
  // its own -- tags is where render_dial.py already renders it.)
  ephemeral                         = var.tags.lifecycle == "ephemeral"
  secret_recovery_window_days       = local.ephemeral ? 0 : 30
  kafka_secret_recovery_window_days = local.ephemeral ? 0 : 30
}

// An SCP fences the test account to one region; nothing fences a deployment to
// the right ACCOUNT, and GCP and Azure have no SCP at all. So the assertion is
// in the config, where all three clouds can carry it.
resource "terraform_data" "account_guard" {
  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.provision.account
      error_message = "This shell is authenticated to AWS account ${data.aws_caller_identity.current.account_id}, but provision.account is ${var.provision.account}. Re-authenticate, or correct the dial."
    }
  }
}

module "naming" {
  source = "../../modules/tf-naming"

  project   = local.project
  component = "cluster"
  env       = var.env
  cloud     = local.cloud
  region    = var.provision.region
}

module "cluster" {
  source = "../../modules/kubernetes-cluster/aws"

  provision = {
    account = var.provision.account
    region  = var.provision.region
    cidr    = var.provision.cidr
  }

  name               = var.name
  env                = var.env
  kubernetes_version = var.kubernetes_version
  node_pools         = var.node_pools
  resolved_shapes    = var.resolved_shapes
  network            = var.network
  endpoint           = var.endpoint
  dns                = var.dns
  telemetry          = var.telemetry
  tags               = var.tags
}

// The one Kafka password of the deployment, generated HERE rather than in the
// secrets module because a managed broker is CREATED with it: the same value has
// to reach the store ESO reads and the secret MSK authenticates against, and a
// value generated inside that module never leaves it -- which is what keeps a
// credential out of the plan and out of every output.
//
// special = false for the reason that module states: the value rides a JAAS
// string, where punctuation needs escaping and buys no entropy at 32 characters.
resource "random_password" "kafka_scram" {
  length  = 32
  special = false

  lifecycle {
    // NEVER regenerated. The brokers keep the password they were created with,
    // so a new one locks every DFE service out of Kafka. Rotation is a
    // deliberate, separate operation.
    ignore_changes = all
  }
}

// Nothing is created here unless the deployment asked for the managed broker.
// strimzi and redpanda run inside the cluster, and a SaaS broker is another body
// under managed-kafka rather than another branch in this root.
module "kafka" {
  count = var.kafka.provider == "msk" ? 1 : 0

  source = "../../modules/managed-kafka/msk"

  name = var.name
  env  = var.env

  network          = module.cluster.network
  eks_cluster_name = module.cluster.cluster_name
  kms_key_arn      = module.cluster.kms_key_arn

  broker_shape_ref = var.kafka.msk.shape_ref
  resolved_shapes  = var.resolved_shapes
  broker_count     = var.kafka.msk.broker_count
  kafka_version    = var.kafka.msk.broker_version

  num_partitions    = var.kafka.msk.num_partitions
  log_retention_ms  = var.kafka.msk.log_retention_ms
  message_max_bytes = var.kafka.msk.message_max_bytes

  secret_recovery_window_days = local.kafka_secret_recovery_window_days

  scram_username = var.kafka.msk.scram_username
  scram_password = random_password.kafka_scram.result

  pod_identity = {
    namespace       = var.kafka.msk.bootstrap_job.namespace
    service_account = var.kafka.msk.bootstrap_job.service_account
  }

  pod_identity_trust_policy_json = module.cluster.pod_identity_trust_policy_json

  autoscaling = var.kafka.msk.autoscaling
  telemetry   = var.telemetry
}

// Confluent Cloud sizes, tunes and versions the cluster itself (CONTRACT.md),
// so this root passes only what no vendor default can stand in for: the name,
// the network the private attachment lands in, and nothing else. The vendor API
// credential is NOT a tfvar -- `provider "confluent"` below reads
// CONFLUENT_CLOUD_API_KEY / CONFLUENT_CLOUD_API_SECRET from the deployer's own
// environment, the way the aws provider already reads AWS_* rather than a var.
module "confluent" {
  count = var.kafka.provider == "confluent-cloud" ? 1 : 0

  source = "../../modules/managed-kafka/confluent-cloud"

  name    = var.name
  env     = var.env
  network = module.cluster.network

  // The same canonical tuning msk applies, and the same landing topics its
  // own bootstrap Job would create -- this body has none, so tofu creates
  // them itself (CONTRACT.md).
  num_partitions    = var.kafka.num_partitions
  log_retention_ms  = var.kafka.log_retention_ms
  message_max_bytes = var.kafka.message_max_bytes
  landing_topics    = var.kafka.landing_topics
}

// Same reasoning as confluent-cloud: Redpanda Cloud Serverless sizes and scales
// itself, so only the name and network travel. The SCRAM password reuses the
// SAME seed msk is created with -- one Kafka password per deployment, whichever
// provider ends up authenticating against it -- and the vendor client
// credential is a provider environment variable (REDPANDA_CLIENT_ID /
// REDPANDA_CLIENT_SECRET), never a tfvar.
module "redpanda" {
  count = var.kafka.provider == "redpanda-cloud" ? 1 : 0

  source = "../../modules/managed-kafka/redpanda-cloud"

  name    = var.name
  env     = var.env
  network = module.cluster.network

  scram_password = random_password.kafka_scram.result

  // The same canonical tuning msk applies, and the same landing topics its
  // own bootstrap Job would create -- this body has none, so tofu creates
  // them itself (CONTRACT.md).
  num_partitions    = var.kafka.num_partitions
  log_retention_ms  = var.kafka.log_retention_ms
  message_max_bytes = var.kafka.message_max_bytes
  landing_topics    = var.kafka.landing_topics

  // The provider defaults this true, which makes tofu destroy refuse. Only an
  // ephemeral deployment gets that: CONTRACT.md's own words are "a customer
  // deployment sets it false", and lifecycle is what tells the two apart.
  allow_deletion = local.ephemeral
}

locals {
  // The managed broker's handles, or empty strings when the deployment runs its
  // own brokers or a different SaaS body. var.kafka.provider selects at most
  // ONE of the three modules, so concatenating every body's output the same way
  // and joining over "at most one populated list" is coalescing, not an
  // ambiguity between vendors -- and the outputs below need no index that may
  // not exist.
  managed_kafka = {
    bootstrap = join("", concat(
      module.kafka[*].bootstrap,
      module.confluent[*].bootstrap,
      module.redpanda[*].bootstrap,
    ))
    bootstrap_iam = join("", concat(
      module.kafka[*].bootstrap_iam,
      module.confluent[*].bootstrap_iam,
      module.redpanda[*].bootstrap_iam,
    ))
    credential_ref = join("", concat(
      module.kafka[*].credential_ref,
      module.confluent[*].credential_ref,
      module.redpanda[*].credential_ref,
    ))
    bootstrap_role_arn = join("", concat(
      module.kafka[*].bootstrap_role_arn,
      module.confluent[*].bootstrap_role_arn,
      module.redpanda[*].bootstrap_role_arn,
    ))
  }
}

module "secrets" {
  source = "../../modules/secrets/aws-sm"

  project = local.project
  env     = var.env
  prefix  = var.secrets.ref
  seeds   = var.seeds

  // The same value a managed broker was created with, so the store and Kafka
  // cannot disagree. An in-cluster broker reads it from the store too, through
  // the ExternalSecret the kafka chart renders.
  kafka_password = random_password.kafka_scram.result

  kms_key_arn                    = module.cluster.kms_key_arn
  cluster_name                   = module.cluster.cluster_name
  pod_identity_trust_policy_json = module.cluster.pod_identity_trust_policy_json

  recovery_window_days = local.secret_recovery_window_days
}
