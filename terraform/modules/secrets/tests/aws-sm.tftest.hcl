// The aws-sm body's deletion-protection knob: recovery_window_days. Provider-
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
      partition = "aws"
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
  pod_identity_trust_policy_json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
}

run "default_recovery_window_is_the_persistent_answer" {
  command = plan

  module {
    source = "./aws-sm"
  }

  assert {
    condition     = aws_secretsmanager_secret.seed["kafka/msk"].recovery_window_in_days == 30
    error_message = "recovery_window_days defaults to 30 -- the vendor default a persistent deployment keeps -- when the caller sets nothing; an ephemeral deployment passes 0 explicitly"
  }
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
