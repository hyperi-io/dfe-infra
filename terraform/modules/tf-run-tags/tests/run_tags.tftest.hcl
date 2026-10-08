// The Python half (scripts/cloud_run.py) writes the same values from the same
// epoch: 1791460800 is 2026-10-08T12:00:00Z.

run "a_deployment_that_is_not_a_run_carries_no_tags" {
  command = plan

  assert {
    condition     = length(output.tags) == 0
    error_message = "a null run must render no tags, or a reaper could act on a persistent deployment"
  }
}

run "a_run_carries_its_id_and_an_iso8601_expiry_by_default" {
  command = plan

  variables {
    run = {
      id         = "r20261008t000000z-0a1b2c"
      expires_at = 1791460800
    }
  }

  assert {
    condition     = output.tags["dfe-e2e"] == "r20261008t000000z-0a1b2c"
    error_message = "the run tag must carry the run id under the default key"
  }

  assert {
    condition     = output.tags["expires-at"] == "2026-10-08T12:00:00Z"
    error_message = "the default expiry format must be ISO-8601 UTC with a Z suffix"
  }

  assert {
    condition     = length(output.tags) == 2
    error_message = "a run adds exactly two tags"
  }
}

run "the_epoch_format_writes_plain_seconds" {
  command = plan

  variables {
    run = {
      id         = "run-1"
      expires_at = 1791460800
      keys       = { format = "epoch" }
    }
  }

  assert {
    condition     = output.tags["expires-at"] == "1791460800"
    error_message = "the epoch format must write plain seconds, the only form a GCP label holds"
  }
}

run "renamed_keys_replace_the_defaults" {
  command = plan

  variables {
    run = {
      id         = "run-1"
      expires_at = 1791460800
      keys       = { run = "ci-run", expiry = "ci-expiry", format = "epoch" }
    }
  }

  assert {
    condition     = output.tags == tomap({ "ci-run" = "run-1", "ci-expiry" = "1791460800" })
    error_message = "renamed keys must replace the defaults, not sit beside them"
  }
}

run "an_uppercase_run_id_is_refused" {
  command = plan

  variables {
    run = {
      id         = "Run-1"
      expires_at = 1791460800
    }
  }

  expect_failures = [var.run]
}

run "a_fractional_expiry_is_refused" {
  command = plan

  variables {
    run = {
      id         = "run-1"
      expires_at = 1791460800.5
    }
  }

  expect_failures = [var.run]
}

run "a_key_starting_with_a_digit_is_refused" {
  command = plan

  variables {
    run = {
      id         = "run-1"
      expires_at = 1791460800
      keys       = { run = "1run" }
    }
  }

  expect_failures = [var.run]
}

run "an_unknown_expiry_format_is_refused" {
  command = plan

  variables {
    run = {
      id         = "run-1"
      expires_at = 1791460800
      keys       = { format = "rfc2822" }
    }
  }

  expect_failures = [var.run]
}
