// The aws-sm body's deletion-protection dial: recovery_window_days. Provider-
// free by construction, like managed-kafka's contracts -- mock_provider means
// no credentials, no API call and no cost.

mock_provider "aws" {
  // A policy document's generated default is a random string, and the
  // provider rejects a policy that is not a JSON object.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  mock_data "aws_region" {
    defaults = {
      region = "us-west-2"
    }
  }

  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "000000000000"
    }
  }

  mock_data "aws_partition" {
    defaults = {
      partition  = "aws"
      dns_suffix = "amazonaws.com"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::000000000000:role/mock"
    }
  }
}

variables {
  project = "dfe"
  env     = "test"

  seeds = {
    "kafka/msk" = { password = "" }
  }

  kafka_password = "contract-only-not-a-real-credential"

  kms_key_arn                    = "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"
  cluster_name                   = "dfe-contract"
  engine_namespace               = "dfe-test"
  pod_identity_trust_policy_json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"

  // No module default: the aws root always sets it from tags.lifecycle.
  recovery_window_days = 30
}

run "the_caller_sets_the_ephemeral_window" {
  command = plan

  module {
    source = "./aws-sm"
  }

  variables {
    recovery_window_days = 0
  }

  assert {
    condition     = aws_secretsmanager_secret.seed["kafka/msk"].recovery_window_in_days == 0
    error_message = "a caller-supplied recovery_window_days of 0 must reach the secret unchanged"
  }
}

run "the_caller_sets_the_persistent_window" {
  command = plan

  module {
    source = "./aws-sm"
  }

  variables {
    recovery_window_days = 30
  }

  assert {
    condition     = aws_secretsmanager_secret.seed["kafka/msk"].recovery_window_in_days == 30
    error_message = "a caller-supplied recovery_window_days must reach the secret unchanged"
  }
}

// ESO reads <store prefix> + <remoteRef key>, and every chart key already
// starts with <project>/<env>. So the store prefix is the ref alone, and the
// secret this module creates is exactly the name that lookup forms.
run "the_store_prefix_names_the_project_and_env_once" {
  command = plan

  module {
    source = "./aws-sm"
  }

  variables {
    prefix = "org-secrets"
  }

  assert {
    condition     = output.store_config.prefix == "org-secrets"
    error_message = "store_config.prefix must be the ref alone -- the chart keys carry <project>/<env> themselves, so the full path here names them twice"
  }

  assert {
    condition     = aws_secretsmanager_secret.seed["kafka/msk"].name == "${output.store_config.prefix}/dfe/test/kafka/msk"
    error_message = "the secret must be named <store prefix>/<project>/<env>/<seed>, the key ESO looks up for the chart's dfe/test/kafka/msk"
  }
}

run "an_empty_ref_leaves_the_chart_keys_absolute" {
  command = plan

  module {
    source = "./aws-sm"
  }

  assert {
    condition     = output.store_config.prefix == ""
    error_message = "with no ref the store prefix is empty, so bootstrap renders none"
  }

  assert {
    condition     = aws_secretsmanager_secret.seed["kafka/msk"].name == "dfe/test/kafka/msk"
    error_message = "with no ref the secret is named exactly as the chart key"
  }
}

run "an_out_of_range_window_is_refused" {
  command = plan

  module {
    source = "./aws-sm"
  }

  variables {
    recovery_window_days = 3
  }

  expect_failures = [var.recovery_window_days]
}

// The gateway chart's PushSecret creates the internal CA root at
// <project>/<env>/pki/internal-ca-crt and -key; every other secret stays read-only to ESO.
run "eso_writes_the_internal_ca_path_and_nothing_else" {
  command = plan

  module {
    source = "./aws-sm"
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.eso.statement :
      length(setsubtract(["secretsmanager:CreateSecret", "secretsmanager:PutSecretValue", "secretsmanager:TagResource", "secretsmanager:DescribeSecret"], s.actions)) == 0
      && s.resources == toset(["arn:aws:secretsmanager:us-west-2:000000000000:secret:dfe/test/pki/*"])
    ])
    error_message = "the external-secrets role must be able to create, write, tag and describe secrets under <path>/pki/ -- the PushSecret that persists the internal CA root needs all four"
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.eso.statement :
      length(setintersection(s.actions, ["secretsmanager:CreateSecret", "secretsmanager:PutSecretValue", "secretsmanager:TagResource"])) == 0
      || alltrue([for r in s.resources : endswith(r, ":secret:dfe/test/pki/*")])
    ])
    error_message = "no write grant may reach a secret outside <path>/pki/ -- the seeds and every other secret stay read-only to ESO"
  }
}

run "the_internal_ca_write_grant_follows_the_store_prefix" {
  command = plan

  module {
    source = "./aws-sm"
  }

  variables {
    prefix = "org-secrets"
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.eso.statement :
      contains(s.actions, "secretsmanager:CreateSecret")
      && s.resources == toset(["arn:aws:secretsmanager:us-west-2:000000000000:secret:org-secrets/dfe/test/pki/*"])
    ])
    error_message = "ESO pushes to <store prefix>/<project>/<env>/pki/internal-ca-crt and -key, so the write grant must carry the prefix too"
  }
}

// MSK fixes its secret's name outside the path, so the root hands its ARN in and
// ESO reads it there, read-only.
run "a_secret_named_outside_the_path_is_read_and_never_written" {
  command = plan

  module {
    source = "./aws-sm"
  }

  variables {
    read_secret_arns = ["arn:aws:secretsmanager:us-west-2:000000000000:secret:AmazonMSK_dfe-contract-AbCdEf"]
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.eso.statement :
      s.actions == toset(["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"])
      && s.resources == toset(["arn:aws:secretsmanager:us-west-2:000000000000:secret:AmazonMSK_dfe-contract-AbCdEf"])
    ])
    error_message = "ESO must be able to read the AmazonMSK_ secret by its ARN"
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.eso.statement :
      !contains(s.resources, "arn:aws:secretsmanager:us-west-2:000000000000:secret:AmazonMSK_dfe-contract-AbCdEf")
      || length(setintersection(s.actions, ["secretsmanager:CreateSecret", "secretsmanager:PutSecretValue", "secretsmanager:TagResource"])) == 0
    ])
    error_message = "the broker's own secret is read, never written, by ESO"
  }
}

run "no_secret_outside_the_path_reads_nothing_more" {
  command = plan

  module {
    source = "./aws-sm"
  }

  assert {
    condition     = length(data.aws_iam_policy_document.eso.statement) == 4
    error_message = "with no read_secret_arns the policy keeps its four statements: path read, engine deny, pki write, key decrypt"
  }
}

// Confluent's API key IS the username, and Confluent mints it, so the caller
// hands both halves in and neither is generated here.
run "a_confluent_seed_takes_the_callers_key_and_secret" {
  command = plan

  module {
    source = "./aws-sm"
  }

  variables {
    seeds = {
      "kafka/confluent-cloud" = { username = "", password = "" }
      "ui/nextauth"           = { secret = "" }
    }

    kafka_username = "AAAAAAAAAAAAAAAA"
  }

  assert {
    condition = jsondecode(aws_secretsmanager_secret_version.seed["kafka/confluent-cloud"].secret_string) == {
      username = "AAAAAAAAAAAAAAAA"
      password = "contract-only-not-a-real-credential"
    }
    error_message = "the kafka/confluent-cloud secret must carry the caller's key as username and the caller's secret as password"
  }

  assert {
    condition     = keys(random_password.seed) == ["ui/nextauth|secret"]
    error_message = "only the UI's own secret is generated here -- a username generated for Confluent could never match the key it minted"
  }
}

run "a_seed_asking_for_a_username_refuses_a_missing_one" {
  command = plan

  module {
    source = "./aws-sm"
  }

  variables {
    seeds = {
      "kafka/confluent-cloud" = { username = "", password = "" }
    }
  }

  expect_failures = [
    var.kafka_username,
  ]
}

// An external ClickHouse already holds its password, so the caller hands it in
// and nothing here generates one.
run "an_external_clickhouse_seed_takes_the_callers_password" {
  command = plan

  module {
    source = "./aws-sm"
  }

  variables {
    seeds = {
      "kafka/msk"           = { password = "" }
      "clickhouse/external" = { password = "" }
    }

    clickhouse_password = "contract-only-not-a-clickhouse-credential"
  }

  assert {
    condition = jsondecode(aws_secretsmanager_secret_version.seed["clickhouse/external"].secret_string) == {
      password = "contract-only-not-a-clickhouse-credential"
    }
    error_message = "the clickhouse/external secret must carry the caller's password"
  }

  assert {
    condition     = length(random_password.seed) == 0
    error_message = "no password is generated for an external ClickHouse, nor for the broker"
  }

  assert {
    condition     = aws_secretsmanager_secret.seed["clickhouse/external"].name == "dfe/test/clickhouse/external"
    error_message = "the secret must be named <project>/<env>/clickhouse/external, the key the clickhouse-cluster chart reads"
  }
}

run "an_external_clickhouse_seed_refuses_a_missing_password" {
  command = plan

  module {
    source = "./aws-sm"
  }

  variables {
    seeds = {
      "kafka/msk"           = { password = "" }
      "clickhouse/external" = { password = "" }
    }
  }

  expect_failures = [
    var.clickhouse_password,
  ]
}

// An account guardrail that refuses CreateRole without a boundary refuses the
// external-secrets role without one.
run "the_eso_role_carries_the_permissions_boundary" {
  command = plan

  module {
    source = "./aws-sm"
  }

  variables {
    permissions_boundary = "arn:aws:iam::000000000000:policy/contract-boundary"
    iam_path             = "/dfe-e2e/"
  }

  assert {
    condition     = aws_iam_role.eso.permissions_boundary == "arn:aws:iam::000000000000:policy/contract-boundary"
    error_message = "the external-secrets aws_iam_role must carry var.permissions_boundary"
  }

  assert {
    condition     = aws_iam_role.eso.path == "/dfe-e2e/"
    error_message = "the external-secrets aws_iam_role must sit under var.iam_path"
  }

  assert {
    condition     = aws_iam_role.engine.permissions_boundary == "arn:aws:iam::000000000000:policy/contract-boundary" && aws_iam_role.engine.path == "/dfe-e2e/"
    error_message = "the dfe-engine aws_iam_role must carry var.permissions_boundary and sit under var.iam_path"
  }
}

// dfe-engine mints its signing key and ClickHouse passwords into the store, so it
// writes, but only under <path>/engine/, and it names each secret rather than listing.
run "the_engine_writes_its_own_prefix_and_nothing_else" {
  command = plan

  module {
    source = "./aws-sm"
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.engine.statement :
      s.effect == "Allow"
      && s.actions == toset([
        "secretsmanager:CreateSecret",
        "secretsmanager:UpdateSecret",
        "secretsmanager:GetSecretValue",
        "secretsmanager:DeleteSecret",
        "secretsmanager:TagResource",
      ])
      && s.resources == toset(["arn:aws:secretsmanager:us-west-2:000000000000:secret:dfe/test/engine/*"])
    ])
    error_message = "the engine role must create, update, read, delete and tag under <path>/engine/ -- the calls the engine's store makes, tags riding on CreateSecret"
  }

  assert {
    condition = length(data.aws_iam_policy_document.engine.statement) == 2 && alltrue([
      for s in data.aws_iam_policy_document.engine.statement :
      !contains(s.actions, "secretsmanager:ListSecrets")
      && alltrue([for a in s.actions : !strcontains(a, "*")])
      && !contains(s.resources, "*")
    ])
    error_message = "the engine role holds two statements, store and key, with no ListSecrets, no wildcard action and no wildcard resource"
  }

  assert {
    condition     = output.engine_name_prefix == "dfe/test/engine"
    error_message = "engine_name_prefix must be <project>/<env>/engine, the prefix the role is fenced to"
  }
}

run "the_engine_prefix_follows_the_store_prefix" {
  command = plan

  module {
    source = "./aws-sm"
  }

  variables {
    prefix = "org-secrets"
  }

  assert {
    condition     = output.engine_name_prefix == "org-secrets/dfe/test/engine"
    error_message = "engine_name_prefix must carry the store prefix, as every name under the path does"
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.engine.statement :
      s.resources == toset(["arn:aws:secretsmanager:us-west-2:000000000000:secret:org-secrets/dfe/test/engine/*"])
    ])
    error_message = "the engine's write grant must follow the store prefix"
  }
}

// The key also wraps EKS Secrets and MSK, and its policy delegates to IAM, so an
// unconditioned grant would decrypt any ciphertext under it.
run "the_engine_uses_the_key_through_secrets_manager_alone" {
  command = plan

  module {
    source = "./aws-sm"
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.engine.statement :
      s.effect == "Allow"
      && s.actions == toset(["kms:GenerateDataKey", "kms:Decrypt"])
      && s.resources == toset(["arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"])
      && anytrue([
        for c in s.condition :
        c.test == "StringEquals" && c.variable == "kms:ViaService"
        && length(c.values) == 1 && contains(c.values, "secretsmanager.us-west-2.amazonaws.com")
      ])
      if length([for a in s.actions : a if startswith(a, "kms:")]) > 0
    ])
    error_message = "the engine's KMS grant must be GenerateDataKey and Decrypt on the deployment key, conditioned on kms:ViaService secretsmanager.<region>"
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.engine.statement :
      anytrue([
        for c in s.condition :
        c.test == "StringLike" && c.variable == "kms:EncryptionContext:SecretARN"
        && length(c.values) == 1 && contains(c.values, "arn:aws:secretsmanager:us-west-2:000000000000:secret:dfe/test/engine/*")
      ])
      if contains(s.actions, "kms:Decrypt")
    ])
    error_message = "the engine's KMS grant must be held to secrets under its own prefix by kms:EncryptionContext:SecretARN"
  }
}

run "the_engine_role_binds_its_own_service_account" {
  command = plan

  module {
    source = "./aws-sm"
  }

  assert {
    condition     = aws_eks_pod_identity_association.engine.namespace == "dfe-test" && aws_eks_pod_identity_association.engine.service_account == "dfe-engine"
    error_message = "the engine role must be associated with the dfe-engine service account in engine_namespace"
  }

  assert {
    condition     = aws_eks_pod_identity_association.engine.cluster_name == "dfe-contract"
    error_message = "the engine association must name the deployment's own cluster"
  }
}

run "eso_is_denied_the_engine_prefix" {
  command = plan

  module {
    source = "./aws-sm"
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.eso.statement :
      s.effect == "Deny"
      && s.actions == toset(["secretsmanager:*"])
      && s.resources == toset(["arn:aws:secretsmanager:us-west-2:000000000000:secret:dfe/test/engine/*"])
    ])
    error_message = "ESO's read grant spans the path, so it must be denied <path>/engine/ explicitly"
  }
}

run "a_seed_under_the_engine_prefix_is_refused" {
  command = plan

  module {
    source = "./aws-sm"
  }

  variables {
    seeds = {
      "kafka/msk"   = { password = "" }
      "engine/oops" = { value = "" }
    }
  }

  expect_failures = [
    var.seeds,
  ]
}
