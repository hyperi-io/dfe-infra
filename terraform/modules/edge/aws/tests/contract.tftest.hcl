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

  // The AL2023 arm64 AMI id this module resolves through the SSM public
  // parameter -- never a literal in the module itself, but the mock has to
  // answer something for the instance resource to plan against.
  mock_data "aws_ssm_parameter" {
    defaults = {
      value = "ami-0123456789abcdef0"
    }
  }

  mock_data "aws_region" {
    defaults = {
      region = "us-west-2"
    }
  }

  mock_resource "aws_security_group" {
    defaults = {
      id = "sg-00000000000forwarder"
    }
  }

  mock_resource "aws_instance" {
    defaults = {
      id = "i-00000000000forwarder"
    }
  }

  // The address is the whole point of the forwarder, and a generated default
  // leaves the output with nothing to assert on.
  mock_resource "aws_eip" {
    defaults = {
      id        = "eipalloc-0000000000000000f"
      public_ip = "198.51.100.42"
    }
  }
}

variables {
  name         = "dfe-edge-contract"
  env          = "test"
  cluster_name = "dfe-edge-contract"

  network = {
    vpc_id            = "vpc-00000000000000000"
    cidr              = "10.90.0.0/16"
    azs               = ["mock-1a", "mock-1b", "mock-1c"]
    public_subnet_ids = ["subnet-0000000000000pub1", "subnet-0000000000000pub2", "subnet-0000000000000pub3"]
  }

  pod_identity_trust_policy_json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
  private_zone_arn               = "arn:aws:route53:::hostedzone/MOCKPRIVATE"
  kms_key_arn                    = "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"

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

// --- the tunnel's address: byo is the default and creates nothing at all

run "byo_is_the_default_and_builds_no_forwarder" {
  command = plan

  assert {
    condition     = length(aws_instance.forwarder) == 0
    error_message = "address.mode byo must render no instance -- the deployer already holds the address"
  }

  assert {
    condition     = length(aws_eip.forwarder) == 0
    error_message = "address.mode byo must allocate no Elastic IP"
  }

  assert {
    condition     = length(aws_security_group.forwarder) == 0
    error_message = "address.mode byo must render no security group"
  }

  assert {
    condition     = length(aws_iam_role.forwarder) == 0
    error_message = "address.mode byo must mint no instance role"
  }

  assert {
    condition     = output.tunnel_address == "" && output.tunnel_zone == ""
    error_message = "both tunnel outputs must be empty on byo -- an address this module never created is not one it can report"
  }
}

// --- the forwarder: one instance, one address, the toolbox's own shape

run "the_forwarder_holds_the_address_and_is_reached_only_through_ssm" {
  command = plan

  variables {
    tunnel = { address = { mode = "forwarder" } }
  }

  assert {
    condition     = length(aws_instance.forwarder) == 1
    error_message = "address.mode forwarder must render exactly one instance"
  }

  assert {
    condition     = aws_instance.forwarder[0].associate_public_ip_address == false
    error_message = "the instance must take no auto-assigned public address -- the Elastic IP is the deployment's address"
  }

  assert {
    condition     = aws_instance.forwarder[0].source_dest_check == false
    error_message = "an instance that rewrites a packet's destination is forwarding traffic it does not own, which EC2's source/destination check refuses by default"
  }

  assert {
    condition     = aws_instance.forwarder[0].metadata_options[0].http_tokens == "required" && aws_instance.forwarder[0].metadata_options[0].http_put_response_hop_limit == 1
    error_message = "IMDSv2 must be required at hop limit 1"
  }

  assert {
    condition     = aws_instance.forwarder[0].root_block_device[0].encrypted == true && aws_instance.forwarder[0].root_block_device[0].kms_key_id == var.kms_key_arn
    error_message = "the root volume must be encrypted with the deployment CMK"
  }

  // The Elastic IP is its own resource, never an `instance` attribute on
  // aws_eip: the address is in every client config already issued, so
  // replacing the instance must not replace it.
  assert {
    condition     = length(aws_eip_association.forwarder) == 1
    error_message = "the address must be attached through an association, so an instance replacement keeps it"
  }

  assert {
    condition     = output.tunnel_address == aws_eip.forwarder[0].public_ip
    error_message = "the tunnel_address output must carry the Elastic IP external-dns publishes vpn.serverCN at"
  }
}

run "the_forwarder_role_reads_the_node_list_and_can_do_nothing_else" {
  command = plan

  variables {
    tunnel = { address = { mode = "forwarder" } }
  }

  assert {
    condition = sort([
      for a in aws_iam_role_policy_attachment.forwarder_ssm : a.policy_arn
    ])[0] == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    error_message = "the instance role must attach AmazonSSMManagedInstanceCore, never ReadOnlyAccess"
  }

  assert {
    condition = length([
      for s in jsondecode(aws_iam_role_policy.forwarder_describe[0].policy).Statement : s
      if s.Action == "ec2:DescribeInstances"
    ]) == 1
    error_message = "the inline policy must grant ec2:DescribeInstances"
  }

  assert {
    condition     = length(jsondecode(aws_iam_role_policy.forwarder_describe[0].policy).Statement) == 1
    error_message = "reading the node list is the only grant this role carries"
  }

  assert {
    condition     = !strcontains(aws_iam_role_policy.forwarder_describe[0].policy, "ec2:CreateTags") && !strcontains(aws_iam_role_policy.forwarder_describe[0].policy, "ec2:*")
    error_message = "the forwarder writes nothing in EC2, so no write or wildcard action may appear"
  }
}

// --- the security group: the tunnel's listeners in, the node ports out, and
// no ssh rule of any kind.

run "the_security_group_admits_both_tunnels_from_everywhere_by_default" {
  command = plan

  variables {
    tunnel = { address = { mode = "forwarder" } }
  }

  assert {
    condition = toset([
      for r in aws_vpc_security_group_ingress_rule.tunnel : r.from_port
    ]) == toset([51820, 1194])
    error_message = "WireGuard and OpenVPN are both exposed by the chart's own listeners list, so both must be admitted"
  }

  assert {
    condition = alltrue([
      for r in aws_vpc_security_group_ingress_rule.tunnel : r.ip_protocol == "udp"
    ])
    error_message = "both tunnel listeners are UDP -- a TCP rule here would open a door nothing listens on"
  }

  // Empty is not "unset": an edge fleet dialling in from anywhere is the normal
  // case, and the chart's own values.yaml says the same of its allow-list.
  assert {
    condition = alltrue([
      for r in aws_vpc_security_group_ingress_rule.tunnel : r.cidr_ipv4 == "0.0.0.0/0"
    ])
    error_message = "an empty source_ranges must admit 0.0.0.0/0 rather than silently admitting nothing"
  }

  assert {
    condition = alltrue([
      for r in aws_vpc_security_group_ingress_rule.tunnel : r.from_port != 22
    ])
    error_message = "no ssh rule of any kind -- every operator reach is an SSM session the instance initiates"
  }

  assert {
    condition = alltrue([
      for r in aws_vpc_security_group_egress_rule.forwarder_nodes : r.cidr_ipv4 == var.network.cidr
    ])
    error_message = "the DNAT target is always a node inside this VPC, so its egress must never reach beyond the CIDR"
  }

  assert {
    condition = toset([
      for r in aws_vpc_security_group_egress_rule.forwarder_nodes : r.from_port
    ]) == toset([31820, 31194])
    error_message = "egress must open the nodePort of every listener the forwarder DNATs to"
  }
}

run "a_named_allow_list_replaces_the_open_default_on_every_listener" {
  command = plan

  variables {
    tunnel = {
      address       = { mode = "forwarder" }
      source_ranges = ["203.0.113.0/24", "198.51.100.0/24"]
    }
  }

  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.tunnel) == 4
    error_message = "two listeners across two ranges must render a rule each, not one range's worth"
  }

  assert {
    condition = length([
      for r in aws_vpc_security_group_ingress_rule.tunnel : r if r.cidr_ipv4 == "0.0.0.0/0"
    ]) == 0
    error_message = "a named allow-list must replace the open default, never sit beside it"
  }
}

run "turning_the_openvpn_listener_off_closes_its_door_end_to_end" {
  command = plan

  variables {
    tunnel = {
      address = { mode = "forwarder" }
      openvpn = false
    }
  }

  assert {
    condition = toset([
      for r in aws_vpc_security_group_ingress_rule.tunnel : r.from_port
    ]) == toset([51820])
    error_message = "with the OpenVPN listener off, 1194 must not be admitted"
  }

  assert {
    condition = toset([
      for r in aws_vpc_security_group_egress_rule.forwarder_nodes : r.from_port
    ]) == toset([31820])
    error_message = "with the OpenVPN listener off, its nodePort must not be opened either"
  }

  assert {
    condition     = !strcontains(aws_instance.forwarder[0].user_data, "1194")
    error_message = "the DNAT programme must carry no rule for a listener the dial closed"
  }
}

// --- the zone: cross-zone transfer is billed per GB, so the instance and
// culvert's pod belong in the same one.

run "the_forwarder_takes_the_networks_first_zone_by_default" {
  command = plan

  variables {
    tunnel = { address = { mode = "forwarder" } }
  }

  assert {
    condition     = output.tunnel_zone == "mock-1a"
    error_message = "an unset zone must take the first availability zone the network spans"
  }

  assert {
    condition     = aws_instance.forwarder[0].subnet_id == var.network.public_subnet_ids[0]
    error_message = "the instance must land in the public subnet belonging to the zone it reports"
  }
}

// An Elastic IP is delivered only where the route table sends 0.0.0.0/0 at an
// internet gateway, which is what separates the two subnet lists.
run "the_forwarder_never_lands_in_a_subnet_outside_the_public_set" {
  command = plan

  variables {
    tunnel = { address = { mode = "forwarder", zone = "mock-1b" } }
  }

  assert {
    condition     = contains(var.network.public_subnet_ids, aws_instance.forwarder[0].subnet_id)
    error_message = "the forwarder's subnet must come from network.public_subnet_ids, or its Elastic IP receives nothing"
  }

  assert {
    condition     = var.network.public_subnet_ids[index(var.network.azs, output.tunnel_zone)] == aws_instance.forwarder[0].subnet_id
    error_message = "the subnet the instance launches in must belong to the zone the deployment publishes to culvert's nodeSelector"
  }
}

run "a_named_zone_selects_its_own_subnet" {
  command = plan

  variables {
    tunnel = { address = { mode = "forwarder", zone = "mock-1c" } }
  }

  assert {
    condition     = output.tunnel_zone == "mock-1c"
    error_message = "a named zone must be the zone the deployment reports to culvert's nodeSelector"
  }

  assert {
    condition     = aws_instance.forwarder[0].subnet_id == var.network.public_subnet_ids[2]
    error_message = "azs and public_subnet_ids are parallel lists, so the named zone selects the matching subnet"
  }
}

run "a_zone_the_vpc_does_not_span_is_refused_by_name" {
  command = plan

  variables {
    tunnel = { address = { mode = "forwarder", zone = "mock-9z" } }
  }

  expect_failures = [
    aws_instance.forwarder,
  ]
}

// --- the instance type is sized by bandwidth, and the family has to match the
// arm64 AMI this module resolves.

run "rejects_a_non_graviton_forwarder_instance_type" {
  command = plan

  variables {
    tunnel = { address = { mode = "forwarder", instance_type = "t3.small" } }
  }

  expect_failures = [
    var.tunnel,
  ]
}

run "rejects_an_address_mode_outside_the_two" {
  command = plan

  variables {
    tunnel = { address = { mode = "elastic" } }
  }

  expect_failures = [
    var.tunnel,
  ]
}

run "rejects_a_node_port_outside_the_kubernetes_range" {
  command = plan

  variables {
    tunnel = {
      address    = { mode = "forwarder" }
      node_ports = { wireguard = 8080 }
    }
  }

  expect_failures = [
    var.tunnel,
  ]
}

// --- the DNAT programme carries the facts it cannot discover for itself

run "the_user_data_carries_the_cluster_tag_and_the_port_map" {
  command = plan

  variables {
    tunnel = { address = { mode = "forwarder" } }
  }

  assert {
    condition     = strcontains(aws_instance.forwarder[0].user_data, "kubernetes.io/cluster/$CLUSTER_NAME")
    error_message = "the refresh must filter on the cluster tag EKS and Karpenter both apply"
  }

  assert {
    condition     = strcontains(aws_instance.forwarder[0].user_data, "CLUSTER_NAME=\"${var.cluster_name}\"")
    error_message = "the instance must be told which cluster's nodes to read"
  }

  assert {
    condition     = strcontains(aws_instance.forwarder[0].user_data, "PORT_MAP=\"51820:31820 1194:31194\"")
    error_message = "the DNAT programme must map each listener port to the nodePort culvert's Service pins"
  }

  assert {
    condition     = strcontains(aws_instance.forwarder[0].user_data, "VPC_CIDR=\"${var.network.cidr}\"")
    error_message = "the return path is masqueraded towards this VPC, so the instance has to know its CIDR"
  }

  // ec2:DescribeInstances takes no resource scope, so the tag alone would let
  // anyone able to tag an instance in the account join the DNAT target set.
  assert {
    condition     = strcontains(aws_instance.forwarder[0].user_data, "Name=vpc-id,Values=$VPC_ID")
    error_message = "the node list must be filtered to this deployment's own VPC, not the whole account"
  }

  // ip_forward is on and the source/destination check is off, so the kernel's
  // own FORWARD ACCEPT would route anything handed to this instance.
  assert {
    condition     = strcontains(aws_instance.forwarder[0].user_data, "iptables -P FORWARD DROP")
    error_message = "the forwarder must route the flows it rewrites and refuse everything else"
  }

  assert {
    condition = strcontains(
      aws_instance.forwarder[0].user_data,
      "iptables -A DFE_TUNNEL_FWD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
    )
    error_message = "a FORWARD default of DROP needs the return half of an established flow accepted"
  }

  // A blanket MASQUERADE to the VPC would cover traffic the DNAT chain never
  // rewrote, which is the same hole one table down.
  assert {
    condition = !strcontains(
      aws_instance.forwarder[0].user_data, "POSTROUTING -d \"$VPC_CIDR\" -j MASQUERADE"
    )
    error_message = "the masquerade must be scoped to the rewritten UDP flows, not the whole VPC"
  }
}

// A replaced instance keeps the address; a stopped and started one keeps the OLD
// DNAT chain, because cloud-init runs this script once per instance.
run "a_changed_port_map_rebuilds_the_instance_rather_than_restarting_it" {
  command = plan

  variables {
    tunnel = { address = { mode = "forwarder" } }
  }

  assert {
    condition     = aws_instance.forwarder[0].user_data_replace_on_change == true
    error_message = "a user_data change must replace the forwarder, or its rules and its security group drift apart"
  }

  // The address is a separate resource and is attached by association, which is
  // what lets the instance be replaced without re-rolling what clients dial.
  assert {
    condition     = aws_eip_association.forwarder[0].allocation_id == aws_eip.forwarder[0].id
    error_message = "the address must attach by association, not as an argument on the instance"
  }
}
