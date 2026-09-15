// The contract, executable. Provider-free by construction: mock_provider
// means no credentials, no API call and no cost, so this runs in CI and on a
// laptop with no cloud account.

mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }

  // The AL2023 arm64 AMI id this module resolves through the SSM public
  // parameter -- never a literal in the module itself, but the mock has to
  // answer something for the instance resource to plan against.
  mock_data "aws_ssm_parameter" {
    defaults = {
      value = "ami-0123456789abcdef0"
    }
  }

  mock_resource "aws_security_group" {
    defaults = {
      id = "sg-0000000000000toolbox"
    }
  }

  mock_resource "aws_instance" {
    defaults = {
      id = "i-0000000000000toolbox"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::000000000000:role/mock-toolbox"
    }
  }

  mock_resource "aws_s3_bucket" {
    defaults = {
      arn = "arn:aws:s3:::dfe-toolbox-contract-toolbox-session-logs"
    }
  }
}

variables {
  name = "dfe-toolbox-contract"
  env  = "test"

  network = {
    vpc_id             = "vpc-00000000000000000"
    cidr               = "10.90.0.0/16"
    private_subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"]
  }

  instance_type = "t4g.small"
  ttl_minutes   = 60

  tool_versions = {
    kubectl                    = "v1.36.0"
    helm                       = "v4.2.4"
    argocd-cli                 = "v3.5.2"
    tofu                       = "1.11.0"
    yq                         = "v4.44.3"
    aws-cli                    = "2.19.0"
    aws-session-manager-plugin = "1.2.707.0"
    clickhouse-client          = "26.3.17.56"
    psql                       = "17"
  }

  session = {
    idle_timeout_minutes = 15
    max_duration_minutes = 60
  }

  session_log_retention_days = 90
  kms_key_arn                = "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"

  // The shape the aws root computes: the API, MSK's SCRAM and IAM listeners,
  // and a ClickHouse the deployer brought. An in-cluster Service name is never
  // a target -- it resolves through CoreDNS and the instance is outside the
  // cluster -- so the root drops it and this fixture never carries one either.
  targets = {
    eks-api    = { host = "ABCDEF1234.gr7.us-west-2.eks.amazonaws.com", port = 443 }
    kafka      = { host = "b-1.mock.kafka.us-west-2.amazonaws.com", port = 9096 }
    kafka-iam  = { host = "b-1.mock.kafka.us-west-2.amazonaws.com", port = 9098 }
    clickhouse = { host = "clickhouse.mock.internal", port = 9440 }
  }

  force_destroy_session_logs = true

  enabled = true

  tags = {
    "service-name"      = "dfe"
    "service-namespace" = "dfe"
    "environment"       = "test"
    "owner"             = "contract-test"
    "cost-center"       = "contract-test"
    "lifecycle"         = "ephemeral"
    "iac-source"        = "dfe-infra/terraform/modules/toolbox/aws"
  }
}

// --- enabled: the instance, zero ingress, IMDSv2, encryption, self-terminate

run "enabled_renders_the_instance_with_no_public_ip_and_no_inbound_rule" {
  command = plan

  assert {
    condition     = length(aws_instance.this) == 1
    error_message = "enabled = true must render exactly one instance"
  }

  assert {
    condition     = aws_instance.this[0].associate_public_ip_address == false
    error_message = "the instance must never take a public IP"
  }

  // The only ingress rule this module declares goes on the EKS control plane's
  // group, never on its own, and only when a caller names that group. Nothing
  // ever reaches the instance itself.
  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.eks_api) == 0
    error_message = "no cluster security group named means no ingress rule anywhere"
  }

  assert {
    condition     = length(aws_vpc_security_group_egress_rule.control) == 1
    error_message = "exactly one control-plane (443) egress rule must exist"
  }

  assert {
    condition     = aws_vpc_security_group_egress_rule.control[0].cidr_ipv4 == "0.0.0.0/0" && aws_vpc_security_group_egress_rule.control[0].from_port == 443 && aws_vpc_security_group_egress_rule.control[0].to_port == 443
    error_message = "control-plane egress must be 443 to 0.0.0.0/0 -- the SSM/ECR/S3 reach that has no VPC interface endpoint here"
  }

  assert {
    condition = alltrue([
      for r in aws_vpc_security_group_egress_rule.targets : r.cidr_ipv4 == var.network.cidr
    ])
    error_message = "every non-443 target egress rule must be scoped to the VPC CIDR, never 0.0.0.0/0"
  }

  assert {
    condition = length([
      for r in aws_vpc_security_group_egress_rule.targets : r if r.from_port == 9096
    ]) == 1
    error_message = "the kafka target's port (9096) must have its own egress rule"
  }

  // MSK's IAM listener is a target of its own, so it gets a rule of its own --
  // nothing opened 9098 while the SCRAM port was the only named target.
  assert {
    condition = length([
      for r in aws_vpc_security_group_egress_rule.targets : r if r.from_port == 9098
    ]) == 1
    error_message = "the kafka-iam target's port (9098) must have its own egress rule"
  }

  // 443 is already open to 0.0.0.0/0 for the SSM control channel, so a target
  // on it adds nothing and must not render a second, narrower rule.
  assert {
    condition = length([
      for r in aws_vpc_security_group_egress_rule.targets : r if r.from_port == 443
    ]) == 0
    error_message = "the eks-api target must not duplicate the control-plane egress rule"
  }

  assert {
    condition     = aws_instance.this[0].metadata_options[0].http_tokens == "required" && aws_instance.this[0].metadata_options[0].http_put_response_hop_limit == 1
    error_message = "IMDSv2 must be required at hop limit 1"
  }

  assert {
    condition     = aws_instance.this[0].root_block_device[0].encrypted == true && aws_instance.this[0].root_block_device[0].kms_key_id == var.kms_key_arn
    error_message = "the root volume must be encrypted with the deployment CMK"
  }

  assert {
    condition     = aws_instance.this[0].instance_initiated_shutdown_behavior == "terminate"
    error_message = "an OS shutdown must terminate the instance -- that is the whole self-terminate mechanism, and it needs no IAM grant"
  }
}

// --- the Kubernetes API is reachable only once the control plane's own group
// admits this instance. Egress on 443 is the client half and was never the
// problem: the cluster group trusts nodes and pods, and a brand-new group is
// neither, so the eks-api forward timed out against an open client side.

run "the_cluster_group_admits_the_toolbox_on_the_api_port" {
  command = plan

  variables {
    eks_cluster_security_group_id = "sg-00000000000000000"
  }

  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.eks_api) == 1
    error_message = "a named cluster security group must get exactly one ingress rule"
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.eks_api[0].security_group_id == var.eks_cluster_security_group_id
    error_message = "the rule goes on the CLUSTER's group, never on the toolbox's own"
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.eks_api[0].referenced_security_group_id == aws_security_group.this[0].id
    error_message = "the grant must name the toolbox's group, never a CIDR -- a CIDR would admit the whole VPC"
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.eks_api[0].from_port == 443 && aws_vpc_security_group_ingress_rule.eks_api[0].to_port == 443
    error_message = "the grant must be the API port alone"
  }
}

// The grant is gated on `enabled` like everything else here, so `bastion down`
// takes it away with the instance rather than leaving a standing hole.
run "the_cluster_group_grant_goes_away_with_the_instance" {
  command = plan

  variables {
    enabled                       = false
    eks_cluster_security_group_id = "sg-00000000000000000"
  }

  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.eks_api) == 0
    error_message = "a disabled toolbox must leave no ingress rule on the cluster's group"
  }
}

// --- the self-terminate design carries no IAM grant that could terminate or
// enumerate sessions -- the instance_initiated_shutdown_behavior setting
// above is the ENTIRE mechanism.

run "the_terminate_permission_is_self_scoped_by_needing_no_iam_at_all" {
  command = plan

  assert {
    condition     = !can(aws_iam_role_policy.session_log[0].policy) || !strcontains(aws_iam_role_policy.session_log[0].policy, "ec2:TerminateInstances")
    error_message = "no policy in this module may grant ec2:TerminateInstances -- self-terminate is instance_initiated_shutdown_behavior + a local process count, not an API call"
  }

  assert {
    condition     = !strcontains(aws_iam_role_policy.session_log[0].policy, "ssm:DescribeSessions")
    error_message = "no policy in this module may grant ssm:DescribeSessions -- it is unscoped (Resource: \"*\") and the idle check does not need it"
  }

  assert {
    condition     = !strcontains(aws_iam_role_policy.session_log[0].policy, "s3:*") && strcontains(aws_iam_role_policy.session_log[0].policy, "s3:PutObject")
    error_message = "the session-log grant must be s3:PutObject only, never a wildcard action"
  }
}

// --- no ReadOnlyAccess -- only the three narrow grants CONTRACT.md names

run "instance_profile_carries_no_read_only_access" {
  command = plan

  assert {
    condition = sort([
      for a in aws_iam_role_policy_attachment.ssm : a.policy_arn
    ])[0] == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    error_message = "the instance role must attach AmazonSSMManagedInstanceCore"
  }

  assert {
    condition = sort([
      for a in aws_iam_role_policy_attachment.ecr_pull : a.policy_arn
    ])[0] == "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPullOnly"
    error_message = "the instance role must attach AmazonEC2ContainerRegistryPullOnly, never ReadOnlyAccess"
  }
}

// --- the session document carries the dial's timeouts

run "the_session_document_carries_the_dials_timeouts" {
  command = plan

  assert {
    condition     = jsondecode(aws_ssm_document.shell[0].content).sessionType == "Standard_Stream"
    error_message = "the shell document must be a Standard_Stream session"
  }

  assert {
    condition     = jsondecode(aws_ssm_document.shell[0].content).inputs.idleSessionTimeout == "15"
    error_message = "idleSessionTimeout must come from session.idle_timeout_minutes"
  }

  assert {
    condition     = jsondecode(aws_ssm_document.shell[0].content).inputs.maxSessionDuration == "60"
    error_message = "maxSessionDuration must come from session.max_duration_minutes"
  }

  assert {
    condition     = jsondecode(aws_ssm_document.shell[0].content).inputs.runAsEnabled == true
    error_message = "shell sessions must not run as root"
  }
}

// --- forward documents: one per target, host/port fixed, no override
// parameter -- P1-1's whole point.

run "forward_documents_fix_host_and_port_with_no_override_parameter" {
  command = plan

  assert {
    condition     = length(aws_ssm_document.forward) == length(var.targets)
    error_message = "one forward document must exist per entry in var.targets"
  }

  assert {
    condition     = jsondecode(aws_ssm_document.forward["eks-api"].content).properties.host == "ABCDEF1234.gr7.us-west-2.eks.amazonaws.com"
    error_message = "the eks-api document must fix the real cluster endpoint host"
  }

  assert {
    condition     = jsondecode(aws_ssm_document.forward["eks-api"].content).properties.portNumber == "443"
    error_message = "the eks-api document must fix port 443"
  }

  assert {
    condition     = !contains(keys(jsondecode(aws_ssm_document.forward["eks-api"].content).parameters), "host")
    error_message = "a forward document must declare NO host parameter -- fixing it in properties with no matching parameter is what makes an override impossible, not merely discouraged"
  }

  assert {
    condition     = !contains(keys(jsondecode(aws_ssm_document.forward["eks-api"].content).parameters), "portNumber")
    error_message = "a forward document must declare NO portNumber parameter, for the same reason"
  }

  assert {
    condition     = contains(keys(jsondecode(aws_ssm_document.forward["eks-api"].content).parameters), "localPortNumber")
    error_message = "localPortNumber is the one parameter a forward document may declare -- it is the operator's own local port, not the target"
  }

  assert {
    condition     = jsondecode(aws_ssm_document.forward["eks-api"].content).sessionType == "Port"
    error_message = "a forward document must be a Port session"
  }
}

run "targets_output_carries_each_documents_name" {
  command = plan

  assert {
    condition     = output.targets["eks-api"].document_name == aws_ssm_document.forward["eks-api"].name
    error_message = "the targets output must name the document dfe-ops bastion forward invokes for each target"
  }

  assert {
    condition     = output.targets["eks-api"].port == 443
    error_message = "the targets output must carry the target's port"
  }
}

// --- disabled: no instance, no security group, no IAM role, no documents --
// but the session-log bucket still exists, because it is not gated by enabled.

run "disabled_renders_no_instance_but_keeps_the_log_bucket" {
  command = plan

  variables {
    enabled = false
  }

  assert {
    condition     = length(aws_instance.this) == 0
    error_message = "enabled = false must render no instance"
  }

  assert {
    condition     = length(aws_security_group.this) == 0
    error_message = "enabled = false must render no security group"
  }

  assert {
    condition     = length(aws_iam_role.this) == 0
    error_message = "enabled = false must render no IAM role"
  }

  assert {
    condition     = length(aws_ssm_document.shell) == 0
    error_message = "enabled = false must render no shell document"
  }

  assert {
    condition     = length(aws_ssm_document.forward) == 0
    error_message = "enabled = false must render no forward document"
  }

  assert {
    condition     = output.instance_id == ""
    error_message = "instance_id must be empty when disabled"
  }

  assert {
    // aws_s3_bucket.session_logs carries no count/for_each at all -- it is a
    // single, always-present object, unlike every count-gated resource above.
    condition     = can(aws_s3_bucket.session_logs.arn)
    error_message = "the session-log bucket must exist even when enabled = false -- it outlives the up/down cycle it records"
  }
}

// --- the rest state (disabled, no versions.yaml toolbox stage yet) must not
// fail validation just because tool_versions is empty -- this module is
// ALWAYS instantiated by the aws root, so the ordinary "toolbox never turned
// on" case has to plan cleanly with no upstream stanza at all.

run "disabled_with_no_tool_versions_still_plans" {
  command = plan

  variables {
    enabled       = false
    tool_versions = {}
  }

  assert {
    condition     = length(aws_instance.this) == 0
    error_message = "a disabled toolbox with no tool_versions must still plan -- the requirement only applies when enabled is true"
  }
}

// --- instance_type must be a Graviton family, matching the arm64 AMI

run "rejects_a_non_graviton_instance_type" {
  command = plan

  variables {
    instance_type = "t3.small"
  }

  expect_failures = [
    var.instance_type,
  ]
}

// --- tool_versions must be complete when the toolbox is enabled

run "rejects_incomplete_tool_versions" {
  command = plan

  variables {
    tool_versions = {
      kubectl = "1.36.0"
    }
  }

  expect_failures = [
    var.tool_versions,
  ]
}

// --- ttl_minutes bounds

run "rejects_ttl_minutes_outside_15_to_480" {
  command = plan

  variables {
    ttl_minutes = 481
  }

  expect_failures = [
    var.ttl_minutes,
  ]
}
