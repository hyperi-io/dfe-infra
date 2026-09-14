// The contract, executable. Every body under this module is asserted HERE, in
// this one file -- a gcp/ or azure/ body adds its own mock provider and a run
// block per section below, with the SAME assertions against the same variables.
// A body that cannot satisfy an assertion has changed the contract rather than
// implemented it, and CONTRACT.md is what changes first.
//
// A module source cannot be a variable, which is why each run block names its
// body rather than the file being parameterised over them.
//
// Provider-free by construction: mock_provider means no credentials, no API
// call and no cost, so it runs in CI and on a laptop with no cloud account.

mock_provider "aws" {
  // Two mocked values the configuration reasons over rather than passes
  // through. The module slices three zones out of this list, and the generated
  // default is an empty one.
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["mock-1a", "mock-1b", "mock-1c"]
    }
  }

  // A policy document's generated default is a random string, and the provider
  // rejects an assume_role_policy that is not a JSON object.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  // The partition is half of every ARN the module builds, and the provider
  // validates the shape of an ARN before it ever calls AWS.
  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
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

  mock_resource "aws_kms_key" {
    defaults = {
      arn = "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"
    }
  }

  // An EventBridge target validates the shape of the ARN it delivers to, so a
  // generated random string is rejected before the plan finishes.
  mock_resource "aws_sqs_queue" {
    defaults = {
      arn = "arn:aws:sqs:us-west-2:000000000000:mock"
    }
  }

  mock_resource "aws_iam_instance_profile" {
    defaults = {
      arn = "arn:aws:iam::000000000000:instance-profile/mock"
    }
  }

  // A computed nested block generates as an EMPTY list, and two contract
  // outputs read into one. Supplying them here is what makes those outputs
  // testable at all.
  mock_resource "aws_eks_cluster" {
    defaults = {
      arn                   = "arn:aws:eks:us-west-2:000000000000:cluster/dfe-contract"
      certificate_authority = [{ data = "bW9jay1jbHVzdGVyLWNh" }]
      identity              = [{ oidc = [{ issuer = "https://oidc.example.invalid/id/MOCK" }] }]
    }
  }

  // The deploying principal aws_eks_access_entry.creator names, so the
  // implicit bootstrap_cluster_creator_admin_permissions grant it replaces is
  // made explicit rather than left to whoever ran apply. Shaped as the STS
  // session ARN every assumed role returns -- SSO permission set, cross-
  // account AssumeRole, instance profile, Lambda -- which is what
  // aws_iam_session_context below has to resolve back to an IAM role ARN.
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "000000000000"
      arn        = "arn:aws:sts::000000000000:assumed-role/mock-deployer/mock-session"
      user_id    = "AROAMOCKMOCKMOCKMOCK"
    }
  }

  // The IAM role ARN, path included, that issued the assumed-role session
  // above -- what EKS's CreateAccessEntry actually accepts.
  mock_data "aws_iam_session_context" {
    defaults = {
      issuer_arn = "arn:aws:iam::000000000000:role/aws-reserved/sso.amazonaws.com/us-west-2/mock-deployer"
    }
  }
}

variables {
  provision = {
    account = "000000000000"
    region  = "us-west-2"
    cidr    = "10.90.0.0/16"
  }

  name               = "dfe-contract"
  env                = "test"
  kubernetes_version = "1.36"

  resolved_shapes = {
    eks-system = {
      instance_types = ["m9g.large", "m8g.large"]
      arch           = "arm64"
    }
  }

  node_pools = {
    system = {
      shape_ref     = "eks-system"
      min_size      = 2
      max_size      = 3
      desired_size  = 2
      capacity_type = "ON_DEMAND"
      disk_gb       = 40
      labels        = {}
      taints        = []
    }
  }

  network  = { nat = "single" }
  endpoint = { public = true, allowed_cidrs = ["198.51.100.10/32"] }
  dns      = { private_zone = "contract.internal", public_zone = "contract.example.com" }

  tags = {
    "service-name"      = "dfe"
    "service-namespace" = "hyperi"
    "environment"       = "test"
    "owner"             = "owner@example.com"
    "cost-center"       = "experiments"
    "lifecycle"         = "throwaway"
    "iac-source"        = "dfe-infra/terraform/modules/kubernetes-cluster/tests"
  }
}

run "aws_cluster_handles" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = output.cluster_name == "dfe-contract"
    error_message = "cluster_name must be the name the caller asked for"
  }

  assert {
    condition     = can(tostring(output.cluster_endpoint))
    error_message = "cluster_endpoint must be a string"
  }

  assert {
    condition     = can(tostring(output.cluster_ca))
    error_message = "cluster_ca must be a string"
  }

  assert {
    condition     = output.cluster_version == "1.36"
    error_message = "cluster_version must report the version the cluster runs"
  }

  assert {
    condition     = can(tostring(output.oidc_issuer))
    error_message = "oidc_issuer must be a string"
  }

  assert {
    condition     = can(tostring(output.node_role_arn))
    error_message = "node_role_arn must be a string"
  }

  assert {
    condition     = can(tostring(output.kms_key_arn))
    error_message = "kms_key_arn must be a string"
  }

  assert {
    condition     = can(jsondecode(output.pod_identity_trust_policy_json)) || output.pod_identity_trust_policy_json != ""
    error_message = "pod_identity_trust_policy_json must be a non-empty policy document"
  }
}

// The deploying principal must be named explicitly rather than implied by
// bootstrap_cluster_creator_admin_permissions.
run "aws_cluster_creator_is_named_not_implied" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = aws_eks_cluster.this.access_config[0].bootstrap_cluster_creator_admin_permissions == false
    error_message = "bootstrap_cluster_creator_admin_permissions must be false -- the explicit access entry below is what makes the admin grant reviewable in a plan"
  }

  assert {
    condition     = aws_eks_access_entry.creator.principal_arn == data.aws_iam_session_context.current.issuer_arn
    error_message = "the deploying principal's access entry must name the session context's issuer_arn -- the IAM role EKS accepts, not the STS session ARN"
  }

  assert {
    condition     = aws_eks_access_entry.creator.principal_arn != data.aws_caller_identity.current.arn
    error_message = "the access entry must never be the raw STS assumed-role session ARN, which EKS's CreateAccessEntry rejects"
  }

  assert {
    condition     = aws_eks_access_entry.creator.type == "STANDARD"
    error_message = "the creator's access entry must be STANDARD -- EC2_LINUX and friends refuse an access policy association"
  }

  assert {
    condition     = aws_eks_access_policy_association.creator_admin.policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
    error_message = "the deploying principal must be associated with the cluster-admin access policy, matching what bootstrap_cluster_creator_admin_permissions used to grant implicitly"
  }

  assert {
    condition     = aws_eks_access_policy_association.creator_admin.access_scope[0].type == "cluster"
    error_message = "the admin grant must be cluster-scoped, matching the old implicit behaviour"
  }
}

// aws_kms_key_policy REPLACES the key's whole policy: the account-root
// delegation to IAM has to survive alongside whatever key_policy_grants adds,
// or every existing IAM-side grant on this key (the cluster role above, ESO's
// role) stops working the moment a caller supplies one.
run "aws_kms_key_policy_keeps_root_and_adds_caller_grants" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    key_policy_grants = [
      {
        sid        = "AllowMockServiceToUseTheKey"
        principals = ["mock.amazonaws.com"]
        actions    = ["kms:Decrypt", "kms:DescribeKey"]
        conditions = [
          { test = "StringEquals", variable = "aws:SourceArn", values = ["arn:aws:mock:us-west-2:000000000000:thing/mock"] },
        ]
      },
    ]
  }

  assert {
    condition     = [for s in jsondecode(aws_kms_key_policy.this.policy).Statement : s if s.Sid == "EnableIAMUserPermissions"][0].Principal.AWS == "arn:aws:iam::000000000000:root"
    error_message = "the key policy must keep the account-root delegation to IAM"
  }

  assert {
    condition     = [for s in jsondecode(aws_kms_key_policy.this.policy).Statement : s if s.Sid == "AllowMockServiceToUseTheKey"][0].Principal.Service[0] == "mock.amazonaws.com"
    error_message = "a caller-supplied key_policy_grants entry must reach the key policy"
  }

  assert {
    condition     = [for s in jsondecode(aws_kms_key_policy.this.policy).Statement : s if s.Sid == "AllowMockServiceToUseTheKey"][0].Condition.StringEquals["aws:SourceArn"] == ["arn:aws:mock:us-west-2:000000000000:thing/mock"]
    error_message = "a caller-supplied condition must reach the rendered statement"
  }
}

// No grants supplied: the policy is exactly the root delegation, so a body
// that needs no extra principal (confluent-cloud, redpanda-cloud) leaves the
// key exactly as AWS's own default would have.
run "aws_kms_key_policy_with_no_grants_is_root_only" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = length(jsondecode(aws_kms_key_policy.this.policy).Statement) == 1
    error_message = "with no key_policy_grants the policy must carry only the root delegation statement"
  }
}

run "aws_network_object" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = output.network.cidr == "10.90.0.0/16"
    error_message = "network.cidr must be the CIDR the caller asked for"
  }

  assert {
    condition     = length(output.network.azs) == 3
    error_message = "network.azs must name the three zones the subnets span"
  }

  assert {
    condition     = length(output.network.private_subnet_ids) == 3
    error_message = "network.private_subnet_ids must carry one subnet per zone"
  }

  assert {
    condition     = length(output.network.public_subnet_ids) == 3
    error_message = "network.public_subnet_ids must carry one subnet per zone"
  }

  assert {
    condition     = can(tostring(output.network.vpc_id))
    error_message = "network.vpc_id must be a string"
  }
}

run "aws_network_az_count_two" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    network = { nat = "single", az_count = 2 }
  }

  assert {
    condition     = length(output.network.azs) == 2
    error_message = "network.az_count = 2 must span exactly two zones"
  }

  assert {
    condition     = length(output.network.private_subnet_ids) == 2
    error_message = "network.private_subnet_ids must carry one subnet per zone at az_count = 2"
  }
}

run "aws_dns_zones" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = can(tostring(output.private_zone_id))
    error_message = "private_zone_id must be a string"
  }

  assert {
    condition     = can(tostring(output.public_zone_id))
    error_message = "public_zone_id must be a string"
  }

  assert {
    condition     = can(tolist(output.public_zone_name_servers))
    error_message = "public_zone_name_servers must be a list -- it is what the parent zone delegates to"
  }
}

// No public zone means no public zone id, no name servers, and no DNS-01
// identity for cert-manager to assume.
run "aws_no_public_zone" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    dns = { private_zone = "contract.internal", public_zone = "" }
  }

  assert {
    condition     = output.public_zone_id == ""
    error_message = "public_zone_id must be empty when no public zone was asked for"
  }

  assert {
    condition     = length(output.public_zone_name_servers) == 0
    error_message = "public_zone_name_servers must be empty when no public zone was asked for"
  }

  assert {
    condition     = length(aws_route53_zone.public) == 0
    error_message = "no public hosted zone may be created when dns.public_zone is empty"
  }

  assert {
    condition     = length(aws_iam_role.cert_manager) == 0
    error_message = "no cert-manager DNS-01 role may be created when there is no public zone"
  }
}

// The private endpoint is always on. The public one is off unless asked for,
// and this is the assertion that keeps it that way.
run "aws_private_endpoint_only" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    endpoint = { public = false, allowed_cidrs = [] }
  }

  assert {
    condition     = aws_eks_cluster.this.vpc_config[0].endpoint_public_access == false
    error_message = "endpoint.public = false must leave the public API endpoint off"
  }

  assert {
    condition     = aws_eks_cluster.this.vpc_config[0].endpoint_private_access == true
    error_message = "the private API endpoint is always on"
  }

  assert {
    condition     = length(aws_eks_cluster.this.vpc_config[0].public_access_cidrs) == 0
    error_message = "no public access CIDRs may be set when the public endpoint is off"
  }
}

// per-az is one gateway per zone; single is one for the whole network. Both
// leave every private subnet with a route out.
run "aws_nat_per_az" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    network = { nat = "per-az" }
  }

  assert {
    condition     = length(aws_nat_gateway.this) == 3
    error_message = "network.nat = per-az must create one NAT gateway per zone"
  }

  assert {
    condition     = length(aws_route_table.private) == 3
    error_message = "there must be one private route table per zone either way"
  }
}

run "aws_nat_single" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = length(aws_nat_gateway.this) == 1
    error_message = "network.nat = single must create exactly one NAT gateway"
  }

  assert {
    condition     = length(aws_route_table.private) == 3
    error_message = "there must be one private route table per zone either way"
  }
}

// Karpenter's handles. The chart cannot be told any of this by a label it reads
// off the cluster, so each one is an output the deployer wires in.
run "aws_karpenter_handles" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = output.karpenter.interruption_queue == "dfe-contract"
    error_message = "the queue is named for the cluster, which is what settings.interruptionQueue is set to"
  }

  assert {
    condition     = output.karpenter.discovery_tag == "dfe-contract"
    error_message = "the discovery tag's value is the cluster name, so two clusters in one network cannot take each other's subnets"
  }

  assert {
    condition     = output.karpenter.instance_profile == "dfe-contract-karpenter-node"
    error_message = "Karpenter is handed a profile this module creates, never a role it would have to build one from"
  }

  assert {
    condition     = can(tostring(output.karpenter.controller_role_arn))
    error_message = "controller_role_arn must be a string"
  }

  assert {
    condition     = can(tostring(output.karpenter.node_role_arn))
    error_message = "node_role_arn must be a string"
  }

  assert {
    condition     = aws_sqs_queue.karpenter.message_retention_seconds == 300
    error_message = "a notice older than five minutes describes an instance that has already gone"
  }

  assert {
    condition     = aws_sqs_queue.karpenter.sqs_managed_sse_enabled == true
    error_message = "the interruption queue is encrypted at rest"
  }
}

// dfe-core created this queue, encrypted it, granted it and alerted on it, and
// then wrote nothing to it -- there was no EventBridge rule anywhere. These
// assertions are what stops that shipping twice.
run "aws_karpenter_interruption_is_wired" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = length(aws_cloudwatch_event_rule.karpenter) == 4
    error_message = "spot interruption, rebalance recommendation, instance state change and AWS Health each need a rule"
  }

  assert {
    condition     = length(aws_cloudwatch_event_target.karpenter) == 4
    error_message = "every rule delivers to the queue, or the rule matches and nothing reads it"
  }

  assert {
    condition     = alltrue([for t in aws_cloudwatch_event_target.karpenter : t.arn == aws_sqs_queue.karpenter.arn])
    error_message = "every target is the interruption queue"
  }
}

// A Karpenter node is self-managed, and authentication_mode is API, so the
// aws-auth ConfigMap is read by nobody and this entry is the only thing that
// lets one register.
run "aws_karpenter_nodes_can_join" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = aws_eks_access_entry.karpenter_node.type == "EC2_LINUX"
    error_message = "the Karpenter node role needs an EC2_LINUX access entry -- EKS makes one for a managed group and none for a self-managed node"
  }

  assert {
    condition     = aws_eks_access_entry.karpenter_node.principal_arn == aws_iam_role.karpenter_node.arn
    error_message = "the access entry names the role the instance profile carries"
  }
}

// Both selector terms in the EC2NodeClass match on this one tag, so a subnet or
// security group missing it is capacity Karpenter cannot see.
run "aws_karpenter_network_is_discoverable" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = alltrue([for s in aws_subnet.private : lookup(s.tags, "karpenter.sh/discovery", "") == "dfe-contract"])
    error_message = "every private subnet carries the discovery tag, or Karpenter has nowhere to launch"
  }

  assert {
    condition     = alltrue([for s in aws_subnet.public : lookup(s.tags, "karpenter.sh/discovery", "") == ""])
    error_message = "no public subnet carries the discovery tag -- nodes are private"
  }

  assert {
    condition     = aws_ec2_tag.karpenter_cluster_security_group.key == "karpenter.sh/discovery"
    error_message = "the cluster security group is tagged for discovery; EKS creates it, so a tag resource is how it is reached"
  }
}

// Cloud nodes are arm64. A shape resolved to anything else is a resolver fault
// and must stop the plan rather than launch an image that cannot boot.
run "aws_rejects_x86_shape" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    resolved_shapes = {
      eks-system = {
        instance_types = ["m7i.large"]
        arch           = "amd64"
      }
    }
  }

  expect_failures = [
    var.resolved_shapes,
  ]
}

// A public API endpoint open to the whole internet is not an allowlist.
run "aws_rejects_open_endpoint" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    endpoint = { public = true, allowed_cidrs = ["0.0.0.0/0"] }
  }

  expect_failures = [
    var.endpoint,
  ]
}

// The governance tag set is a contract input, so a missing key fails here
// rather than surfacing months later as an unattributable bill.
run "aws_rejects_incomplete_tags" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    tags = {
      "service-name" = "dfe"
    }
  }

  expect_failures = [
    var.tags,
  ]
}

// EKS has no S3 or OTel export for control-plane logs, so audit stays in
// CloudWatch under BOTH sinks -- otel just pins its retention to the 1-day
// floor since it is an unavoidable exception, not a chosen destination.
run "aws_audit_log_is_the_only_stream_enabled" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    // enabled_cluster_log_types is a set: no index, so contains() is the
    // membership check and length() is what proves nothing else is enabled.
    condition     = length(aws_eks_cluster.this.enabled_cluster_log_types) == 1 && contains(aws_eks_cluster.this.enabled_cluster_log_types, "audit")
    error_message = "only the audit stream may be enabled -- api/controllerManager/scheduler would be CloudWatch cost with nothing downstream to read them"
  }

  assert {
    condition     = aws_cloudwatch_log_group.cluster.retention_in_days == 1
    error_message = "the otel default must pin the audit log group to the 1-day floor"
  }

  assert {
    condition     = aws_cloudwatch_log_group.cluster.name == "/aws/eks/dfe-contract/cluster"
    error_message = "the log group must be pre-created under EKS's own naming convention, so EKS never auto-creates one with no expiration"
  }
}

// The opt-in AWS-native path keeps the audit log at the dial's retention,
// exactly like every other CloudWatch touchpoint under that sink.
run "aws_audit_log_follows_cloudwatch_retention" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    telemetry = {
      sink           = "cloudwatch"
      retention_days = 7
    }
  }

  assert {
    condition     = aws_cloudwatch_log_group.cluster.retention_in_days == 7
    error_message = "the cloudwatch sink must carry telemetry.retention_days, not the otel floor"
  }
}

// With no configuration_values at all, network-policies' whole chart is
// decorative on this cluster -- nothing enforces a NetworkPolicy object.
run "aws_vpc_cni_enables_network_policy" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = jsondecode(aws_eks_addon.this["vpc-cni"].configuration_values).enableNetworkPolicy == "true"
    error_message = "the vpc-cni add-on must set enableNetworkPolicy: \"true\" in its configuration_values, or every NetworkPolicy in the cluster is unenforced"
  }

  // Not asserted here: that the other add-ons' configuration_values stays
  // null. A mocked plan synthesises a placeholder for any optional attribute
  // the config sets to null, so a null-equality check on a mocked resource
  // proves nothing either way -- only a real plan/apply shows the true value.
}

// No operator_role_arn means no toolbox access entry -- the default for every
// caller that has never heard of the toolbox.
run "aws_toolbox_operator_defaults_to_no_access_entry" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = length(aws_eks_access_entry.toolbox_operator) == 0
    error_message = "an empty toolbox_operator_role_arn must create no access entry at all"
  }

  assert {
    condition     = length(aws_eks_access_policy_association.toolbox_operator_view) == 0
    error_message = "an empty toolbox_operator_role_arn must create no access policy association either"
  }
}

// A named operator role gets a SECOND, read-only access entry -- never the
// cluster-admin scope the creator's own entry carries, and never one for the
// toolbox instance itself (which is a different module and creates none).
run "aws_toolbox_operator_gets_read_only_access_entry" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    toolbox_operator_role_arn = "arn:aws:iam::000000000000:role/dfe-toolbox-operator"
  }

  assert {
    condition     = aws_eks_access_entry.toolbox_operator[0].principal_arn == "arn:aws:iam::000000000000:role/dfe-toolbox-operator"
    error_message = "the access entry must name the operator role the caller passed in"
  }

  assert {
    condition     = aws_eks_access_entry.toolbox_operator[0].type == "STANDARD"
    error_message = "the operator's access entry must be STANDARD, matching the creator's"
  }

  assert {
    condition     = aws_eks_access_policy_association.toolbox_operator_view[0].policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"
    error_message = "the toolbox operator must be read-only (AmazonEKSViewPolicy), never cluster-admin"
  }

  assert {
    condition     = aws_eks_access_policy_association.toolbox_operator_view[0].access_scope[0].type == "cluster"
    error_message = "the operator's view grant must be cluster-scoped, matching the creator's admin grant"
  }

  assert {
    // The creator's entry is the only cluster-admin grant; the operator's is
    // read-only. Two entries total, never a third for the toolbox instance,
    // which this module has no input for at all.
    condition     = aws_eks_access_policy_association.creator_admin.policy_arn != aws_eks_access_policy_association.toolbox_operator_view[0].policy_arn
    error_message = "the toolbox operator must never share the creator's cluster-admin policy"
  }
}
