// The edge module's contract, executable. Provider-free by construction:
// mock_provider means no credentials, no API call and no cost, so this runs in
// CI and on a laptop with no cloud account.

mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }

  // A policy document's generated default is a random string, and the provider
  // rejects an assume_role_policy that is not a JSON object.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  mock_resource "aws_iam_policy" {
    defaults = {
      arn = "arn:aws:iam::000000000000:policy/mock"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::000000000000:role/mock"
    }
  }

  // name_servers is a computed list, and it is the whole reason the public zone
  // must never be recreated -- a generated default leaves nothing to assert on.
  mock_resource "aws_route53_zone" {
    defaults = {
      arn          = "arn:aws:route53:::hostedzone/MOCKPUBLIC"
      zone_id      = "MOCKPUBLIC"
      name_servers = ["ns-1.mock.invalid", "ns-2.mock.invalid"]
    }
  }
}

variables {
  name         = "dfe-edge-contract"
  env          = "test"
  cluster_name = "dfe-edge-contract"
  vpc_id       = "vpc-00000000000000000"

  pod_identity_trust_policy_json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
  private_zone_arn               = "arn:aws:route53:::hostedzone/MOCKPRIVATE"

  dns = { public_zone = "contract.example.com" }

  tags = {
    "service-name"      = "dfe"
    "service-namespace" = "hyperi"
    "environment"       = "test"
    "owner"             = "owner@example.com"
    "cost-center"       = "experiments"
    "lifecycle"         = "ephemeral"
    "iac-source"        = "dfe-infra/terraform/modules/edge/aws"
  }
}

// --- the load balancer controller: no cloud door exists without it

run "the_load_balancer_controller_identity_lands_where_its_chart_looks" {
  command = plan

  assert {
    condition     = aws_eks_pod_identity_association.lbc.cluster_name == var.cluster_name
    error_message = "the association must target the cluster the caller named -- Pod Identity resolves by cluster, namespace and service account"
  }

  assert {
    condition     = aws_eks_pod_identity_association.lbc.namespace == "kube-system"
    error_message = "the namespace must match the controller chart's own default"
  }

  assert {
    condition     = aws_eks_pod_identity_association.lbc.service_account == "aws-load-balancer-controller"
    error_message = "the service account must match the controller chart's own default"
  }

  assert {
    condition     = aws_eks_pod_identity_association.lbc.role_arn == aws_iam_role.lbc.arn
    error_message = "the association must name the role this module mints, not any other"
  }

  assert {
    condition     = aws_iam_role.lbc.assume_role_policy == var.pod_identity_trust_policy_json
    error_message = "cluster trust comes from the cluster module's own output, never rebuilt here"
  }
}

// --- the public zone and the two identities that write it

run "the_public_zone_carries_the_name_it_was_given_and_a_delegation" {
  command = plan

  assert {
    condition     = length(aws_route53_zone.public) == 1
    error_message = "a named public zone must render exactly one hosted zone"
  }

  assert {
    condition     = aws_route53_zone.public[0].name == var.dns.public_zone
    error_message = "the zone must carry the name the dial asked for"
  }

  assert {
    condition     = length(output.public_zone_name_servers) > 0
    error_message = "the name servers output is what the parent zone delegates to, so it must be populated whenever a zone exists"
  }

  assert {
    condition     = output.public_zone_id == aws_route53_zone.public[0].zone_id
    error_message = "the zone id output must name the zone this module creates"
  }
}

// external-dns is ONE controller writing both zones, so its grant has to reach
// the private zone the cluster module owns as well as the public one here.
run "external_dns_may_write_both_zones_and_list_nothing_it_cannot" {
  command = plan

  assert {
    condition     = contains(local.zone_arns, var.private_zone_arn)
    error_message = "external-dns must be granted the private zone by ARN -- without it every internal record silently never happens"
  }

  assert {
    condition     = contains(local.zone_arns, aws_route53_zone.public[0].arn)
    error_message = "external-dns must be granted the public zone it is deployed beside"
  }

  assert {
    condition     = aws_eks_pod_identity_association.external_dns.namespace == "external-dns"
    error_message = "the namespace must match argocd/appsets/layer1-addons.yaml's external-dns destination"
  }

  assert {
    condition     = aws_eks_pod_identity_association.external_dns.service_account == "external-dns"
    error_message = "the service account must match the serviceAccount.name argocd/appsets/layer1-addons.yaml sets, never the chart's release-derived default"
  }

  assert {
    condition     = aws_eks_pod_identity_association.external_dns.role_arn == aws_iam_role.external_dns.arn
    error_message = "the association must name the role this module mints, not any other"
  }
}

// cert-manager's DNS-01 write is the public zone alone. The private zone's
// names are internal and are never proved to a public CA.
run "cert_manager_writes_the_public_zone_and_not_the_private_one" {
  command = plan

  assert {
    condition     = length(aws_iam_role.cert_manager) == 1
    error_message = "a named public zone must mint the DNS-01 role"
  }

  assert {
    condition     = local.public_zone_arns == [aws_route53_zone.public[0].arn]
    error_message = "the DNS-01 grant must be scoped to the public zone alone"
  }

  assert {
    condition     = !contains(local.public_zone_arns, var.private_zone_arn)
    error_message = "the DNS-01 grant must never reach the private zone"
  }

  assert {
    condition     = aws_eks_pod_identity_association.cert_manager[0].namespace == "cert-manager"
    error_message = "the namespace must match the release bootstrap.sh installs cert-manager under"
  }

  assert {
    condition     = output.cert_manager_role_arn == aws_iam_role.cert_manager[0].arn
    error_message = "the role ARN output must name the role this module mints"
  }
}

// --- no public zone: the controller stays, the public half goes

run "no_public_zone_leaves_the_controller_and_removes_the_public_half" {
  command = plan

  variables {
    dns = { public_zone = "" }
  }

  assert {
    condition     = length(aws_route53_zone.public) == 0
    error_message = "no public hosted zone may be created when dns.public_zone is empty"
  }

  assert {
    condition     = length(aws_iam_role.cert_manager) == 0
    error_message = "no cert-manager DNS-01 role may be created when there is no public zone"
  }

  assert {
    condition     = length(aws_eks_pod_identity_association.cert_manager) == 0
    error_message = "no cert-manager association may be created when there is no role for it to name"
  }

  assert {
    condition     = output.public_zone_id == "" && length(output.public_zone_name_servers) == 0
    error_message = "both public-zone outputs must be empty when no public zone was asked for"
  }

  // The controller is the cloud's door mechanism, not a DNS one: a deployment
  // with no public name still renders internal LoadBalancer Services.
  assert {
    condition     = can(aws_iam_role.lbc.arn)
    error_message = "the load balancer controller's identity must survive a deployment with no public zone"
  }

  // external-dns still writes the private zone, so its identity is not gated on
  // a public zone either.
  assert {
    condition     = local.zone_arns == [var.private_zone_arn]
    error_message = "with no public zone external-dns must be granted the private zone and nothing else"
  }
}

// --- the vendored policy document, not a plan-time fetch

run "the_controller_policy_is_vendored_rather_than_fetched" {
  command = plan

  assert {
    condition     = can(jsondecode(aws_iam_policy.lbc.policy))
    error_message = "the controller policy must parse as JSON from the file this module vendors"
  }

  assert {
    condition     = jsondecode(aws_iam_policy.lbc.policy).Version == "2012-10-17"
    error_message = "the vendored document must be a policy, not whatever a failed fetch left behind"
  }
}
