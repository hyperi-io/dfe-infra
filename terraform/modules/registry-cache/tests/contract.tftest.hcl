// The contract, executable. A gcp/ or azure/ body adds its own mock provider and
// run blocks with the same assertions. Provider-free: mock_provider means no
// credential, no API call and no cost.

mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = {
      partition  = "aws"
      dns_suffix = "amazonaws.com"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::000000000000:role/dfe-contract-ecr-cache"
    }
  }
}

variables {
  provision = {
    account = "000000000000"
    region  = "us-west-2"
  }

  name             = "dfe-contract"
  namespace        = "dfe-contract"
  pull_expiry_days = 90
  kms_key_arn      = "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"

  node_role_arns = {
    managed   = "arn:aws:iam::000000000000:role/dfe-e2e/dfe-contract-nodes"
    karpenter = "arn:aws:iam::000000000000:role/dfe-contract-karpenter-node"
  }

  upstreams = {
    docker-hub = { upstream_url = "registry-1.docker.io", host = "docker.io", requires_credential = true }
    ghcr = {
      upstream_url        = "ghcr.io"
      host                = "ghcr.io"
      requires_credential = true
      credential_arn      = "arn:aws:secretsmanager:us-west-2:000000000000:secret:ecr-pullthroughcache/ghcr-AbCdEf"
    }
    quay       = { upstream_url = "quay.io", host = "quay.io", requires_credential = false }
    ecr-public = { upstream_url = "public.ecr.aws", host = "public.ecr.aws", requires_credential = false }
  }

  tags = {
    "service-name" = "dfe"
    "environment"  = "test"
    "dfe-e2e"      = "run-1"
    "expires-at"   = "2026-10-08T12:00:00Z"
  }
}

// One rule per upstream that can be cached, under the deployment's own namespace.
run "aws_rules_cover_every_cacheable_upstream" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = sort(keys(aws_ecr_pull_through_cache_rule.this)) == tolist(["ecr-public", "ghcr", "quay"])
    error_message = "every upstream with no credential requirement, or with its credential, must get a rule"
  }

  assert {
    condition     = aws_ecr_pull_through_cache_rule.this["quay"].ecr_repository_prefix == "dfe-contract/quay"
    error_message = "a rule's prefix must sit under the namespace, so two deployments in one registry cannot collide"
  }

  assert {
    condition     = aws_ecr_pull_through_cache_rule.this["ghcr"].credential_arn == var.upstreams["ghcr"].credential_arn
    error_message = "an authenticated upstream's rule must carry the credential the caller named"
  }

  assert {
    condition     = output.uncached == tolist(["docker-hub"])
    error_message = "an upstream that needs a credential and has none must be reported uncached"
  }
}

// The upstream that cannot be cached gets no mirror, so its pulls never meet a cache that refuses them.
run "aws_node_script_mirrors_only_what_is_cached" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = output.mirrors["quay.io"] == "https://000000000000.dkr.ecr.us-west-2.amazonaws.com/v2/dfe-contract/quay"
    error_message = "a mirror must name the ECR path its rule serves, which override_path hands containerd as the API root"
  }

  assert {
    condition     = !contains(keys(output.mirrors), "docker.io")
    error_message = "an uncached upstream must get no mirror"
  }

  assert {
    condition     = strcontains(output.node_user_data, "mirror 'quay.io' 'https://quay.io' 'https://000000000000.dkr.ecr.us-west-2.amazonaws.com/v2/dfe-contract/quay'")
    error_message = "the node script must write quay.io's mirror with quay.io itself as the fallback server"
  }

  assert {
    condition     = !strcontains(output.node_user_data, "docker.io")
    error_message = "the node script must not mirror an uncached upstream"
  }

  assert {
    condition     = strcontains(output.node_user_data, "aws ecr get-login-password --region 'us-west-2'")
    error_message = "the node script must log in to the deployment's own region"
  }

  // The header sits under the mirror's own host table, which keeps the upstream fallback anonymous.
  assert {
    condition     = strcontains(output.node_user_data, "[host.\"$3\".header]")
    error_message = "the token must ride the mirror entry alone"
  }
}

// Repositories ECR creates carry the deployment key, its tags and a pull-age expiry.
run "aws_templates_shape_every_cached_repository" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = alltrue([for prefix, template in aws_ecr_repository_creation_template.this : template.prefix == aws_ecr_pull_through_cache_rule.this[prefix].ecr_repository_prefix])
    error_message = "each rule needs a template on its own prefix"
  }

  assert {
    condition     = alltrue([for template in aws_ecr_repository_creation_template.this : template.encryption_configuration[0].kms_key == var.kms_key_arn && template.encryption_configuration[0].encryption_type == "KMS"])
    error_message = "every cached repository must be encrypted with the deployment key"
  }

  assert {
    condition     = alltrue([for template in aws_ecr_repository_creation_template.this : template.image_tag_mutability == "MUTABLE"])
    error_message = "cached tags must stay mutable, or ECR can never refresh one"
  }

  assert {
    condition     = alltrue([for template in aws_ecr_repository_creation_template.this : template.resource_tags["dfe-e2e"] == "run-1" && template.resource_tags["expires-at"] == "2026-10-08T12:00:00Z"])
    error_message = "every cached repository must carry the caller's tags, both run tags included, which default_tags never reaches"
  }

  assert {
    condition     = output.repository_tags["quay"] == tomap(var.tags)
    error_message = "repository_tags must report what the template stamps"
  }

  assert {
    condition     = jsondecode(aws_ecr_repository_creation_template.this["quay"].lifecycle_policy).rules[0].selection.countType == "sinceImagePulled"
    error_message = "the lifecycle rule must expire by last pull, so an image still in use is kept"
  }

  assert {
    condition     = jsondecode(aws_ecr_repository_creation_template.this["quay"].lifecycle_policy).rules[0].selection.countNumber == 90
    error_message = "the lifecycle rule must take pull_expiry_days"
  }
}

// Every grant names the cached repositories, never the whole registry.
run "aws_grants_are_fenced_to_the_cache" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = sort(keys(aws_iam_role_policy.node_import)) == tolist(["karpenter", "managed"])
    error_message = "both node roles must be able to import through the cache"
  }

  assert {
    condition     = aws_iam_role_policy.node_import["managed"].role == "dfe-contract-nodes"
    error_message = "the policy must attach to the role's name, path stripped"
  }

  assert {
    condition = jsondecode(aws_iam_role_policy.node_import["managed"].policy).Statement[0].Resource == [
      "arn:aws:ecr:us-west-2:000000000000:repository/dfe-contract/ecr-public/*",
      "arn:aws:ecr:us-west-2:000000000000:repository/dfe-contract/ghcr/*",
      "arn:aws:ecr:us-west-2:000000000000:repository/dfe-contract/quay/*",
    ]
    error_message = "a node's import grant must reach only the cached repositories"
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.node_import["managed"].policy).Statement[0].Action == ["ecr:BatchImportUpstreamImage", "ecr:CreateRepository"]
    error_message = "a node needs exactly the two actions a first pull through the cache makes"
  }

  assert {
    condition     = [for s in jsondecode(aws_iam_role_policy.repository_creation[0].policy).Statement : s.Resource if s.Sid == "EncryptWithTheDeploymentKey"][0] == var.kms_key_arn
    error_message = "the creation role's key grant must name the deployment key alone"
  }

  assert {
    condition     = jsondecode(aws_iam_role.repository_creation[0].assume_role_policy).Statement[0].Principal.Service == "ecr.amazonaws.com"
    error_message = "the creation role is assumed by ECR and nothing else"
  }
}

// The destroy-time step empties exactly the rules this deployment made.
run "aws_teardown_captures_the_rule_prefixes" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = terraform_data.repository_teardown[0].input.prefixes == "dfe-contract/ecr-public dfe-contract/ghcr dfe-contract/quay"
    error_message = "the teardown must capture every rule prefix and no other"
  }

  assert {
    condition     = terraform_data.repository_teardown[0].input.region == "us-west-2"
    error_message = "the teardown must run in the deployment's own region"
  }
}

// Nothing cacheable means nothing created and nothing written onto a node.
run "aws_nothing_cacheable_creates_nothing" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    upstreams = {
      docker-hub = { upstream_url = "registry-1.docker.io", host = "docker.io", requires_credential = true }
    }
  }

  assert {
    condition     = length(aws_ecr_pull_through_cache_rule.this) == 0 && length(aws_iam_role.repository_creation) == 0 && length(aws_iam_role_policy.node_import) == 0
    error_message = "with no cacheable upstream the body must create nothing"
  }

  assert {
    condition     = output.node_user_data == ""
    error_message = "with no cacheable upstream no node may run the mirror script"
  }
}

// ECR caps a rule prefix at 30 characters.
run "aws_rejects_a_namespace_that_overflows_a_prefix" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    namespace = "dfe-customer-example-prod"
  }

  expect_failures = [var.upstreams]
}

// ECR's pull-through role reads ecr-pullthroughcache/ secrets and no others.
run "aws_rejects_a_credential_outside_the_ecr_prefix" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    upstreams = {
      ghcr = {
        upstream_url        = "ghcr.io"
        host                = "ghcr.io"
        requires_credential = true
        credential_arn      = "arn:aws:secretsmanager:us-west-2:000000000000:secret:dfe/ghcr-AbCdEf"
      }
    }
  }

  expect_failures = [var.upstreams]
}
