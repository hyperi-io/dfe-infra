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

  // A node group validates that its launch template id starts with lt-, so a
  // generated random string fails the plan.
  mock_resource "aws_launch_template" {
    defaults = {
      id             = "lt-0123456789abcdef0"
      latest_version = 1
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
  dns      = { private_zone = "contract.internal" }

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

  // The group a caller has to be admitted by to reach the API -- the toolbox
  // module puts its single 443 ingress rule on exactly this handle.
  assert {
    condition     = output.cluster_security_group_id == aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
    error_message = "cluster_security_group_id must be the group EKS created for the control plane"
  }

  assert {
    condition     = output.cluster_version == "1.36"
    error_message = "cluster_version must report the version the cluster runs"
  }

  // What dates a deployment: the control plane's own creation stamp, read
  // from the resource rather than kept anywhere this repo would have to
  // maintain.
  assert {
    condition     = output.cluster_created_at == aws_eks_cluster.this.created_at
    error_message = "cluster_created_at must be the control plane's own creation stamp"
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

  // The SHAPE, not the mock's literal: the two asserts above are satisfied by
  // any value the mock happens to differ on, including a bare role name.
  assert {
    condition     = startswith(aws_eks_access_entry.creator.principal_arn, "arn:${data.aws_partition.current.partition}:iam::") && strcontains(aws_eks_access_entry.creator.principal_arn, ":role/")
    error_message = "the access entry must be an IAM role ARN -- the only shape CreateAccessEntry accepts"
  }

  // The policy association carries its own principal_arn, and a half-revert
  // that leaves it on the caller identity fails at AssociateAccessPolicy.
  assert {
    condition     = aws_eks_access_policy_association.creator_admin.principal_arn == aws_eks_access_entry.creator.principal_arn
    error_message = "the admin association must name the same principal as the access entry it grants against"
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
    condition     = can(tostring(output.private_zone_arn))
    error_message = "private_zone_arn must be a string -- it is how the edge module's external-dns role is granted the internal half"
  }

  assert {
    condition     = output.private_zone_arn == aws_route53_zone.private.arn
    error_message = "private_zone_arn must name the zone this module creates, not a reconstructed ARN"
  }
}

// A private hosted zone not associated with a VPC resolves for nothing, and the
// failure is silent: the zone exists, the records exist, and every lookup falls
// through to public DNS instead.
run "aws_private_zone_is_bound_to_the_vpc_it_resolves_in" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = aws_route53_zone.private.name == var.dns.private_zone
    error_message = "the private zone must carry the name the dial asked for"
  }

  assert {
    condition     = length(aws_route53_zone.private.vpc) == 1
    error_message = "the private zone must be associated with exactly one VPC"
  }

  assert {
    condition     = tolist(aws_route53_zone.private.vpc)[0].vpc_id == aws_vpc.this.id
    error_message = "the private zone must be associated with THIS deployment's VPC -- an unassociated private zone resolves for nothing and fails silently"
  }
}

// The private half is unconditional. Whether a deployment publishes public
// names is the edge module's question, and nothing here changes with the
// answer.
run "aws_private_zone_stands_alone_whatever_the_deployment_publishes" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    dns = { private_zone = "other.internal" }
  }

  assert {
    condition     = aws_route53_zone.private.name == "other.internal"
    error_message = "the private zone follows the dial, with no public zone anywhere in this module to depend on"
  }

  assert {
    condition     = can(tostring(output.private_zone_id))
    error_message = "private_zone_id must still be a string with no public zone in the picture"
  }

  assert {
    condition     = output.private_zone_arn == aws_route53_zone.private.arn
    error_message = "private_zone_arn must still name this module's own zone"
  }
}

// The destroy-time cleanup that empties the private zone of everything but
// its own apex NS/SOA pair before `tofu destroy` reaches the zone itself.
// Its provisioner block cannot be asserted here directly -- a destroy-time
// command may reference only self (dns.tf), so the fact that matters is
// what gets captured into that self in the first place. The provisioner's
// own shape (when = destroy, the zone id flowing from self.output) is
// proven instead by scripts/tests/test_external_dns_teardown.py, which
// reads dns.tf as text.
run "aws_private_zone_teardown_captures_the_zone_to_empty" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = terraform_data.private_zone_teardown.input.zone_id == aws_route53_zone.private.zone_id
    error_message = "the destroy-time cleanup must capture THIS deployment's own zone id, not a reconstructed one"
  }

  assert {
    condition     = terraform_data.private_zone_teardown.input.region == var.provision.region
    error_message = "the destroy-time cleanup must run its aws CLI calls against the deployment's own region, not wherever the operator's default happens to point"
  }

  assert {
    condition     = terraform_data.private_zone_teardown.triggers_replace[0] == aws_route53_zone.private.zone_id
    error_message = "a zone replacement must replace the cleanup resource too, or a renamed zone is emptied under the OLD zone id"
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

// EC2 checks the launching principal's own KMS permissions before honouring an
// EC2NodeClass's encrypted blockDeviceMappings -- the controller role that
// calls RunInstances/CreateFleet, never the node role the instance assumes.
run "aws_karpenter_controller_can_use_the_deployment_key_for_ebs_encryption" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = aws_iam_role_policy.karpenter_kms.role == aws_iam_role.karpenter.name
    error_message = "the KMS grant must be on the controller role, not the node role"
  }

  assert {
    condition = [
      for s in jsondecode(aws_iam_role_policy.karpenter_kms.policy).Statement : s.Action if s.Sid == "AllowEBSEncryptionActions"
    ][0] == ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
    error_message = "the direct-use statement must name exactly the actions AWS documents for a service that launches EC2 instances with a customer managed key"
  }

  assert {
    condition = [
      for s in jsondecode(aws_iam_role_policy.karpenter_kms.policy).Statement : s.Action if s.Sid == "AllowEBSEncryptionGrants"
    ][0] == "kms:CreateGrant"
    error_message = "the grant statement must name exactly kms:CreateGrant, never a wider action"
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.karpenter_kms.policy).Statement : s.Resource == aws_kms_key.this.arn
    ])
    error_message = "both statements must be scoped to this deployment's own key, never a wildcard resource"
  }

  assert {
    condition = [
      for s in jsondecode(aws_iam_role_policy.karpenter_kms.policy).Statement :
      s.Condition.StringEquals["kms:ViaService"] if s.Sid == "AllowEBSEncryptionActions"
    ][0] == "ec2.${var.provision.region}.amazonaws.com"
    error_message = "the direct-use statement must be usable only when EC2 is the caller, never the controller calling KMS directly"
  }

  assert {
    condition = [
      for s in jsondecode(aws_iam_role_policy.karpenter_kms.policy).Statement :
      s.Condition.StringEquals["kms:ViaService"] if s.Sid == "AllowEBSEncryptionGrants"
    ][0] == "ec2.${var.provision.region}.amazonaws.com"
    error_message = "the grant statement must also be usable only when EC2 is the caller"
  }

  assert {
    condition = [
      for s in jsondecode(aws_iam_role_policy.karpenter_kms.policy).Statement :
      s.Condition.Bool["kms:GrantIsForAWSResource"] if s.Sid == "AllowEBSEncryptionGrants"
    ][0] == "true"
    error_message = "the grant statement must be restricted to a grant EC2 creates for itself, matching AWS's own required key-policy condition on CreateGrant"
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

// The ClickHouse object-store bucket: encrypted, unreachable from the
// internet, and torn down according to the same lifecycle dial as the KMS
// deletion window above -- never a fixed answer either way.
run "aws_clickhouse_object_store_bucket_is_locked_down" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = aws_s3_bucket_public_access_block.clickhouse_object_store.block_public_acls == true
    error_message = "the object-store bucket must block public ACLs"
  }

  assert {
    condition     = aws_s3_bucket_public_access_block.clickhouse_object_store.block_public_policy == true
    error_message = "the object-store bucket must block public bucket policies"
  }

  assert {
    condition     = aws_s3_bucket_public_access_block.clickhouse_object_store.ignore_public_acls == true
    error_message = "the object-store bucket must ignore any public ACL that somehow gets set"
  }

  assert {
    condition     = aws_s3_bucket_public_access_block.clickhouse_object_store.restrict_public_buckets == true
    error_message = "the object-store bucket must restrict public bucket policies"
  }

  assert {
    // rule and apply_server_side_encryption_by_default are both sets of
    // objects (no addressable index), so one() is what reaches inside.
    condition     = one(one(aws_s3_bucket_server_side_encryption_configuration.clickhouse_object_store.rule).apply_server_side_encryption_by_default).sse_algorithm == "aws:kms"
    error_message = "the object-store bucket must be SSE-KMS, not SSE-S3"
  }

  assert {
    condition     = one(one(aws_s3_bucket_server_side_encryption_configuration.clickhouse_object_store.rule).apply_server_side_encryption_by_default).kms_master_key_id == aws_kms_key.this.arn
    error_message = "the object-store bucket must be encrypted with the deployment's own key -- the same one EKS secrets and MSK use"
  }

  assert {
    condition     = one(aws_s3_bucket_ownership_controls.clickhouse_object_store.rule).object_ownership == "BucketOwnerEnforced"
    error_message = "ACLs must be disabled outright, not left to whatever the account default happens to be"
  }

  // The bucket policy exists to REFUSE, never to grant: a public-access block
  // cannot stop a request that already has access arriving over plaintext.
  assert {
    condition = alltrue([
      for s in jsondecode(aws_s3_bucket_policy.clickhouse_object_store.policy).Statement : s.Effect == "Deny"
    ])
    error_message = "every statement on the object-store bucket policy must be a Deny -- this policy grants nothing"
  }

  assert {
    condition = [
      for s in jsondecode(aws_s3_bucket_policy.clickhouse_object_store.policy).Statement :
      s.Condition.Bool["aws:SecureTransport"] if s.Sid == "DenyInsecureTransport"
    ][0] == "false"
    error_message = "the bucket must refuse a request that arrives over plaintext HTTP"
  }

  // No versioning assertion here: this module declares no
  // aws_s3_bucket_versioning resource for this bucket at all (unlike
  // cloudtrail.tf's own bucket in the root), so there is nothing to reference
  // -- the absence itself is the contract (object-store.tf, "No versioning").

  assert {
    condition     = aws_s3_bucket_lifecycle_configuration.clickhouse_object_store.rule[0].abort_incomplete_multipart_upload[0].days_after_initiation == 7
    error_message = "an abandoned multipart upload must be aborted after 7 days, or a killed insert leaves the bucket growing forever"
  }

  // A Disabled rule, or one with no filter block, is present and inert -- the
  // days above would still assert green while nothing was ever aborted.
  assert {
    condition     = aws_s3_bucket_lifecycle_configuration.clickhouse_object_store.rule[0].status == "Enabled"
    error_message = "the abort rule must be Enabled, not merely declared"
  }

  assert {
    condition     = length(aws_s3_bucket_lifecycle_configuration.clickhouse_object_store.rule[0].filter) == 1
    error_message = "the abort rule needs its empty filter block, or it matches no object at all"
  }

  // S3's namespace is global and var.name comes from the dial, so the name has
  // to be both unique and legal before the apply finds out.
  assert {
    condition     = length(aws_s3_bucket.clickhouse_object_store.bucket) <= 63 && can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", aws_s3_bucket.clickhouse_object_store.bucket))
    error_message = "the bucket name must be a legal S3 name -- lowercase, 3-63 characters, no underscore"
  }

  assert {
    condition     = strcontains(aws_s3_bucket.clickhouse_object_store.bucket, var.provision.account)
    error_message = "the bucket name must carry the account id, or two deployments of the same dial name collide in S3's global namespace"
  }

  assert {
    condition     = aws_s3_bucket.clickhouse_object_store.tags.Name == aws_s3_bucket.clickhouse_object_store.bucket
    error_message = "the Name tag must be the bucket's own name, not a second spelling of it"
  }

  // The default test tags carry lifecycle = "throwaway", not "ephemeral" --
  // the persistent branch, so force_destroy must be off.
  assert {
    condition     = aws_s3_bucket.clickhouse_object_store.force_destroy == false
    error_message = "a deployment whose tags.lifecycle is not \"ephemeral\" must keep the safer default and refuse force_destroy"
  }
}

// An ephemeral (tyre-kick) deployment gets the fast, no-confirmation teardown
// on this bucket too -- the same dial the KMS deletion window and the root's
// CloudTrail/toolbox buckets already follow.
run "aws_clickhouse_object_store_force_destroy_follows_lifecycle" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    tags = {
      "service-name"      = "dfe"
      "service-namespace" = "hyperi"
      "environment"       = "test"
      "owner"             = "owner@example.com"
      "cost-center"       = "experiments"
      "lifecycle"         = "ephemeral"
      "iac-source"        = "dfe-infra/terraform/modules/kubernetes-cluster/tests"
    }
  }

  assert {
    condition     = aws_s3_bucket.clickhouse_object_store.force_destroy == true
    error_message = "tags.lifecycle = ephemeral must force_destroy the object-store bucket, matching the KMS deletion window's own rule"
  }
}

// The Pod Identity role's policy must name only this bucket (and the
// deployment key it is encrypted with) -- never a wildcard, never another
// bucket, or a compromised pod could read or overwrite something else.
run "aws_clickhouse_object_store_role_is_scoped_to_its_own_bucket" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = [for s in jsondecode(aws_iam_role_policy.clickhouse_object_store.policy).Statement : s.Resource if s.Sid == "ListBucket"][0] == aws_s3_bucket.clickhouse_object_store.arn
    error_message = "ListBucket must be scoped to this bucket's own ARN"
  }

  assert {
    condition     = [for s in jsondecode(aws_iam_role_policy.clickhouse_object_store.policy).Statement : s.Resource if s.Sid == "ReadWriteObjects"][0] == "${aws_s3_bucket.clickhouse_object_store.arn}/*"
    error_message = "object read/write must be scoped to this bucket's own objects"
  }

  assert {
    condition     = [for s in jsondecode(aws_iam_role_policy.clickhouse_object_store.policy).Statement : s.Resource if s.Sid == "UseTheDeploymentKey"][0] == aws_kms_key.this.arn
    error_message = "the KMS grant must be scoped to the deployment's own key"
  }

  // That key also wraps EKS Secrets and MSK, and its key policy delegates to
  // IAM -- so without ViaService the grant reaches any ciphertext under it,
  // not just this bucket's.
  assert {
    condition = [
      for s in jsondecode(aws_iam_role_policy.clickhouse_object_store.policy).Statement :
      s.Condition.StringEquals["kms:ViaService"] if s.Sid == "UseTheDeploymentKey"
    ][0] == "s3.${var.provision.region}.amazonaws.com"
    error_message = "the KMS grant must be usable only through S3, never called directly"
  }

  assert {
    // No fourth statement, and no statement's Resource is anything but this
    // bucket, its objects, or the deployment key -- the policy names only
    // what this role needs and nothing else in the account.
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.clickhouse_object_store.policy).Statement :
      contains(
        [
          aws_s3_bucket.clickhouse_object_store.arn,
          "${aws_s3_bucket.clickhouse_object_store.arn}/*",
          aws_kms_key.this.arn,
        ],
        s.Resource
      )
    ])
    error_message = "the policy must name only this bucket, its objects and the deployment key -- nothing else"
  }

  // Scoping the Resource is half the job: "s3:*" on this bucket alone would
  // still hand a compromised pod the bucket policy and its own KMS grants, and
  // every Resource assertion above would stay green.
  assert {
    condition     = [for s in jsondecode(aws_iam_role_policy.clickhouse_object_store.policy).Statement : s.Action if s.Sid == "ListBucket"][0] == "s3:ListBucket"
    error_message = "ListBucket must be that one action, never a wildcard"
  }

  assert {
    condition = [
      for s in jsondecode(aws_iam_role_policy.clickhouse_object_store.policy).Statement : s.Action if s.Sid == "ReadWriteObjects"
    ][0] == ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    error_message = "the object statement must name exactly the four verbs the S3 disk uses"
  }

  assert {
    condition = [
      for s in jsondecode(aws_iam_role_policy.clickhouse_object_store.policy).Statement : s.Action if s.Sid == "UseTheDeploymentKey"
    ][0] == ["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"]
    error_message = "the KMS statement must name exactly what SSE-KMS needs of the caller, never kms:*"
  }
}

// The association has to target the namespace and service account the
// clickhouse-cluster chart actually renders its pods into, or Pod Identity
// grants a role nothing authenticates as.
run "aws_clickhouse_object_store_association_targets_the_chart_default" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = aws_eks_pod_identity_association.clickhouse_object_store.cluster_name == aws_eks_cluster.this.name
    error_message = "the association must target this cluster"
  }

  assert {
    condition     = aws_eks_pod_identity_association.clickhouse_object_store.namespace == "clickhouse"
    error_message = "the default namespace must match argocd/appsets/layer2-data.yaml's clickhouse-cluster destination namespace"
  }

  assert {
    condition     = aws_eks_pod_identity_association.clickhouse_object_store.service_account == "dfe-clickhouse"
    error_message = "the default service account must match the chart's clickhouse.serviceAccount.name -- a dedicated account, not the release namespace's default, which every other pod there shares"
  }

  assert {
    condition     = aws_eks_pod_identity_association.clickhouse_object_store.role_arn == aws_iam_role.clickhouse_object_store.arn
    error_message = "the association must name the role this file mints, not any other"
  }
}

// A caller with a chart deployed under a different namespace or service
// account can still point Pod Identity at it.
run "aws_clickhouse_object_store_association_honours_overrides" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    clickhouse_object_store_namespace       = "data"
    clickhouse_object_store_service_account = "clickhouse"
  }

  assert {
    condition     = aws_eks_pod_identity_association.clickhouse_object_store.namespace == "data"
    error_message = "a caller-supplied namespace must reach the association"
  }

  assert {
    condition     = aws_eks_pod_identity_association.clickhouse_object_store.service_account == "clickhouse"
    error_message = "a caller-supplied service account must reach the association"
  }
}

// clickhouse_object_store_endpoint is the value clickhouse.objectStore.endpoint
// actually takes -- the S3 disk config's own trailing-slash requirement
// (values.yaml) makes a missing slash a render-time contract break downstream.
run "aws_clickhouse_object_store_endpoint_shape" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = endswith(output.clickhouse_object_store_endpoint, "/")
    error_message = "the endpoint must carry a trailing slash -- the chart's S3 disk config requires one"
  }

  assert {
    condition     = strcontains(output.clickhouse_object_store_endpoint, output.clickhouse_object_store_bucket)
    error_message = "the endpoint must name the bucket this file provisions"
  }

  // The S3 disk signs for whatever region the host names; a wrong one fails
  // every read and write at runtime rather than at plan.
  assert {
    condition     = strcontains(output.clickhouse_object_store_endpoint, ".s3.${var.provision.region}.amazonaws.com/")
    error_message = "the endpoint's region must be the deployment's own"
  }

  assert {
    condition     = strcontains(output.clickhouse_object_store_endpoint, "/dfe/")
    error_message = "the endpoint must carry the dfe/ prefix every other derived object-store path uses"
  }
}

// An account guardrail that refuses CreateRole without a boundary, outside one
// IAM path, or an S3 write outside one bucket prefix refuses the first resource
// that misses, so EVERY role, instance profile and bucket carries all three.
run "aws_guardrail_inputs_reach_every_role_profile_and_bucket" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    permissions_boundary = "arn:aws:iam::000000000000:policy/contract-boundary"
    iam_path             = "/dfe-e2e/"
    s3_bucket_prefix     = "dfe-e2e-"
  }

  assert {
    condition = alltrue([
      for role in [
        aws_iam_role.cluster,
        aws_iam_role.nodes,
        aws_iam_role.ebs_csi,
        aws_iam_role.karpenter_node,
        aws_iam_role.karpenter,
        aws_iam_role.clickhouse_object_store,
      ] : role.permissions_boundary == "arn:aws:iam::000000000000:policy/contract-boundary" && role.path == "/dfe-e2e/"
    ])
    error_message = "every aws_iam_role in this module must carry var.permissions_boundary and var.iam_path"
  }

  assert {
    condition     = aws_iam_instance_profile.karpenter_node.path == "/dfe-e2e/"
    error_message = "Karpenter's node instance profile must sit under var.iam_path"
  }

  assert {
    condition     = startswith(aws_s3_bucket.clickhouse_object_store.bucket, "dfe-e2e-")
    error_message = "the object-store bucket must carry var.s3_bucket_prefix"
  }
}

// What the add-ons create themselves never sees default_tags, so a guardrail
// that refuses an untagged create needs the run's tags in their configuration.
run "aws_controller_tags_reach_the_addons" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    controller_tags = {
      "dfe-e2e"    = "run-1"
      "expires-at" = "2026-10-08T12:00:00Z"
    }
  }

  assert {
    condition     = jsondecode(aws_eks_addon.this["aws-ebs-csi-driver"].configuration_values).controller.extraVolumeTags["dfe-e2e"] == "run-1"
    error_message = "the EBS CSI driver must tag every volume it provisions with controller_tags"
  }

  assert {
    condition     = jsondecode(jsondecode(aws_eks_addon.this["vpc-cni"].configuration_values).env.ADDITIONAL_ENI_TAGS)["expires-at"] == "2026-10-08T12:00:00Z"
    error_message = "the VPC CNI must tag every pod network interface it creates with controller_tags"
  }

  assert {
    condition     = jsondecode(aws_eks_addon.this["vpc-cni"].configuration_values).enableNetworkPolicy == "true"
    error_message = "adding controller_tags must not drop enableNetworkPolicy"
  }
}

// Adding a launch template to a node group that has none replaces the group, so
// every deployment that is not a test run must plan exactly the shape it had.
run "aws_node_groups_carry_no_launch_template_outside_a_run" {
  command = plan

  module {
    source = "./aws"
  }

  assert {
    condition     = length(aws_launch_template.nodes) == 0
    error_message = "no controller_tags must create no launch template"
  }

  assert {
    condition     = length(aws_eks_node_group.this["system"].launch_template) == 0
    error_message = "no controller_tags must leave the node group with no launch_template block"
  }

  assert {
    condition     = aws_eks_node_group.this["system"].disk_size == 40
    error_message = "with no launch template the node group must size its own disk from disk_gb"
  }
}

// EKS copies no node group tag onto the instances and volumes it launches, so
// during a test run a launch template is what carries the run's tags there.
run "aws_run_tags_reach_node_group_instances_and_volumes" {
  command = plan

  module {
    source = "./aws"
  }

  variables {
    controller_tags = {
      "dfe-e2e"    = "run-1"
      "expires-at" = "2026-10-08T12:00:00Z"
    }
  }

  assert {
    condition     = length(aws_launch_template.nodes) == length(var.node_pools)
    error_message = "every node pool must get its own launch template during a run"
  }

  assert {
    condition = toset([
      for t in aws_launch_template.nodes["system"].tag_specifications : t.resource_type
      if t.tags["dfe-e2e"] == "run-1" && t.tags["expires-at"] == "2026-10-08T12:00:00Z"
    ]) == toset(["instance", "volume"])
    error_message = "the launch template must tag both the instance and its volumes with controller_tags"
  }

  assert {
    condition     = aws_launch_template.nodes["system"].block_device_mappings[0].device_name == "/dev/xvda" && aws_launch_template.nodes["system"].block_device_mappings[0].ebs[0].volume_size == 40
    error_message = "the launch template must size the AL2023 root device from disk_gb, since EKS refuses disk_size beside it"
  }

  assert {
    condition     = aws_launch_template.nodes["system"].metadata_options[0].http_tokens == "required" && aws_launch_template.nodes["system"].metadata_options[0].http_put_response_hop_limit == 1
    error_message = "run nodes must keep IMDSv2 at hop limit 1, not the launch template's EKS default of 2"
  }

  assert {
    condition     = aws_eks_node_group.this["system"].launch_template[0].id == aws_launch_template.nodes["system"].id
    error_message = "the node group must launch through its own pool's template"
  }

  assert {
    condition     = aws_eks_node_group.this["system"].launch_template[0].version == tostring(aws_launch_template.nodes["system"].latest_version)
    error_message = "the node group must follow the template's latest version"
  }
}
