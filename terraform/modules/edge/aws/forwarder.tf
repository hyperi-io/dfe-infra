// The tunnel's address on AWS, tier 2 and opt-in.
//
// culvert is a NodePort on this cloud, because a LoadBalancer in front of a
// fleet tunnel bills per GB processed on traffic that is already the whole
// point of the deployment (argocd/values/edge-aws.yaml). A NodePort has no
// address of its own, so `address.mode: byo` expects one the deployer brings,
// and `forwarder` is this module creating one: an Elastic IP on a small
// instance that DNATs to whichever cluster nodes are running.
//
// The instance follows terraform/modules/toolbox/aws throughout -- the same
// always-latest AL2023 arm64 AMI lookup, the same SSM-managed access with no
// inbound ssh rule, and the same narrow instance role.
//
// The instance lands in a PUBLIC subnet, whose route table sends 0.0.0.0/0 at
// the internet gateway -- an Elastic IP is delivered nowhere else, and a
// private subnet's NAT gateway route would leave the address answering nothing
// with every resource reporting healthy.
//
// The nodePort hop is admitted on the group EKS attaches to a managed node
// group's instances (`node_security_group_id`), whose ingress is otherwise its
// own members alone -- the rule names this instance's own group rather than a
// CIDR, so the hole is one instance wide and closes when the forwarder does.
//
// NOT YET PROVEN AGAINST A CLUSTER. Every hop is now built, and what a live run
// has to show is the DNAT path end to end -- a client dialling the Elastic IP
// reaching culvert's pod -- and the zone placement holding, so the forwarder
// and that pod stay in the one availability zone.

data "aws_region" "current" {}

// Always-latest alias, deliberately NOT pinned to a dated release the way
// karpenter-pools.amiAlias is -- the same reasoning toolbox/aws/main.tf states,
// and this instance is likewise rebuilt rather than carried through a long life.
data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

locals {
  forwarder_enabled = var.tunnel.address.mode == "forwarder"

  // The zone the instance pins to. A hop to a node in another zone crosses a
  // boundary billed per GB, so the deployment puts culvert's pod in this same
  // zone through the nodeSelector the cluster secret's annotation feeds.
  forwarder_zone = var.tunnel.address.zone != "" ? var.tunnel.address.zone : try(var.network.azs[0], "")

  // azs and public_subnet_ids are parallel lists in the cluster module's own
  // network output, so the zone selects the subnet.
  forwarder_subnet_id = try(
    var.network.public_subnet_ids[index(var.network.azs, local.forwarder_zone)],
    "",
  )

  // The listener ports culvert exposes, each with the nodePort its Service
  // pins. The names and the listen ports match helm/edge/culvert/values.yaml's
  // `listeners` list, which is the SSoT for both.
  tunnel_ports = concat(
    [{ name = "wireguard", port = 51820, node_port = var.tunnel.node_ports.wireguard }],
    var.tunnel.openvpn ? [{ name = "openvpn-udp", port = 1194, node_port = var.tunnel.node_ports.openvpn }] : [],
  )

  // Empty is not "unset" -- it is 0.0.0.0/0, and an edge fleet dialling in from
  // anywhere is the normal case (helm/edge/culvert/values.yaml says the same).
  tunnel_source_ranges = length(var.tunnel.source_ranges) > 0 ? var.tunnel.source_ranges : ["0.0.0.0/0"]

  // One ingress rule per listener per allowed range, so a range added to the
  // dial opens every listener rather than only the first.
  tunnel_ingress = {
    for pair in setproduct(local.tunnel_ports, local.tunnel_source_ranges) :
    "${pair[0].name}-${pair[1]}" => { port = pair[0].port, cidr = pair[1] }
  }

  // 443 for the SSM control channel and the EC2 API, neither of which sits
  // behind a VPC interface endpoint here -- the same trade toolbox/aws makes.
  forwarder_control_egress_port = 443

  // What the refresh script is told, as one string per listener.
  forwarder_port_map = join(" ", [for p in local.tunnel_ports : "${p.port}:${p.node_port}"])
}

// ---------------------------------------------------------------------------
// Network -- inbound is the tunnel's own UDP listeners and nothing else. There
// is no ssh rule of any kind: every operator reach is an SSM session initiated
// FROM the instance.
// ---------------------------------------------------------------------------

resource "aws_security_group" "forwarder" {
  count = local.forwarder_enabled ? 1 : 0

  name        = "${var.name}-tunnel-forwarder"
  description = "Tunnel forwarder for ${var.name} (${var.env}) -- UDP tunnel listeners in, no ssh rule of any kind"
  vpc_id      = var.network.vpc_id

  tags = merge(var.tags, { Name = "${var.name}-tunnel-forwarder" })
}

resource "aws_vpc_security_group_ingress_rule" "tunnel" {
  for_each = local.forwarder_enabled ? local.tunnel_ingress : {}

  security_group_id = aws_security_group.forwarder[0].id

  cidr_ipv4   = each.value.cidr
  ip_protocol = "udp"
  from_port   = each.value.port
  to_port     = each.value.port

  description = "Tunnel listener ${each.value.port}/udp from ${each.value.cidr}"
}

resource "aws_vpc_security_group_egress_rule" "forwarder_control" {
  count = local.forwarder_enabled ? 1 : 0

  security_group_id = aws_security_group.forwarder[0].id

  cidr_ipv4   = "0.0.0.0/0"
  ip_protocol = "tcp"
  from_port   = local.forwarder_control_egress_port
  to_port     = local.forwarder_control_egress_port

  description = "SSM control channel and the EC2 API -- neither sits behind a VPC interface endpoint here"
}

// The forwarded half. Scoped to the VPC CIDR, because the DNAT target is always
// a node inside this network.
resource "aws_vpc_security_group_egress_rule" "forwarder_nodes" {
  for_each = local.forwarder_enabled ? { for p in local.tunnel_ports : p.name => p } : {}

  security_group_id = aws_security_group.forwarder[0].id

  cidr_ipv4   = var.network.cidr
  ip_protocol = "udp"
  from_port   = each.value.node_port
  to_port     = each.value.node_port

  description = "DNAT to the ${each.key} nodePort on this deployment's nodes"
}

// The one rule this module puts on a group it does not own, and the hop the
// forwarder is useless without: an open client side reaches a node that drops
// the packet, because the group EKS attaches to a managed node group admits its
// own members alone.
//
// Referenced by group id rather than by CIDR, so the hole names this one
// instance rather than the public subnet it shares with anything else the
// deployer launches, and it goes away with the forwarder on a return to `byo`.
// One rule covers a Karpenter node too: kubernetes-cluster/aws/karpenter.tf
// tags this same group for node discovery, and the refresh script targets any
// instance carrying the cluster tag.
// UDP alone: every listener in tunnel_ports is UDP and the refresh script's DNAT
// chain is `-p udp` throughout, so a TCP rule here would admit a port nothing
// forwards to. The chart's OpenVPN TCP fallback is a commented-out listener
// (helm/edge/culvert/values.yaml) and travels through neither.
resource "aws_vpc_security_group_ingress_rule" "node_port" {
  for_each = local.forwarder_enabled ? { for p in local.tunnel_ports : p.name => p } : {}

  security_group_id = var.node_security_group_id

  referenced_security_group_id = aws_security_group.forwarder[0].id
  ip_protocol                  = "udp"
  from_port                    = each.value.node_port
  to_port                      = each.value.node_port

  description = "${each.key} nodePort from the ${var.name} tunnel forwarder"

  tags = merge(var.tags, { Name = "${var.name}-tunnel-forwarder-${each.key}" })
}

// ---------------------------------------------------------------------------
// Identity -- AmazonSSMManagedInstanceCore, plus ec2:DescribeInstances and
// nothing else. The refresh loop reads the node list and writes nothing, so a
// broader grant would buy the forwarder no capability it uses.
// ---------------------------------------------------------------------------

resource "aws_iam_role" "forwarder" {
  count = local.forwarder_enabled ? 1 : 0

  name = "${var.name}-tunnel-forwarder"
  path = var.iam_path

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })

  permissions_boundary = var.permissions_boundary

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "forwarder_ssm" {
  count = local.forwarder_enabled ? 1 : 0

  role       = aws_iam_role.forwarder[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "forwarder_describe" {
  count = local.forwarder_enabled ? 1 : 0

  name = "${var.name}-tunnel-forwarder-describe"
  role = aws_iam_role.forwarder[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "ReadTheNodeList"
      Effect = "Allow"
      // ec2:DescribeInstances takes no resource argument, so it cannot be
      // scoped to this cluster's nodes and the filter is in the caller instead.
      Action   = "ec2:DescribeInstances"
      Resource = "*"
    }]
  })
}

resource "aws_iam_instance_profile" "forwarder" {
  count = local.forwarder_enabled ? 1 : 0

  name = "${var.name}-tunnel-forwarder"
  path = var.iam_path
  role = aws_iam_role.forwarder[0].name
}

// ---------------------------------------------------------------------------
// The address and the instance
// ---------------------------------------------------------------------------
//
// The Elastic IP is a resource of its own rather than an `instance` attribute
// on aws_eip, so replacing the instance keeps the address: it is in every
// client config already issued, and a new one strands the whole fleet.

resource "aws_eip" "forwarder" {
  count = local.forwarder_enabled ? 1 : 0

  domain = "vpc"

  tags = merge(var.tags, { Name = "${var.name}-tunnel-forwarder" })
}

resource "aws_eip_association" "forwarder" {
  count = local.forwarder_enabled ? 1 : 0

  allocation_id = aws_eip.forwarder[0].id
  instance_id   = aws_instance.forwarder[0].id
}

resource "aws_instance" "forwarder" {
  count = local.forwarder_enabled ? 1 : 0

  ami           = data.aws_ssm_parameter.al2023_arm64.value
  instance_type = var.tunnel.address.instance_type
  subnet_id     = local.forwarder_subnet_id

  vpc_security_group_ids = [aws_security_group.forwarder[0].id]
  iam_instance_profile   = aws_iam_instance_profile.forwarder[0].name

  // The Elastic IP above is the deployment's address, so the auto-assigned one
  // this public subnet could hand out is refused rather than left to churn on
  // every replacement.
  associate_public_ip_address = false

  // An instance that rewrites a packet's destination is forwarding traffic it
  // does not own, which EC2's source/destination check refuses by default.
  source_dest_check = false

  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 20
    encrypted             = true
    kms_key_id            = var.kms_key_arn
    delete_on_termination = true
  }

  user_data = templatefile("${path.module}/templates/forwarder.sh.tftpl", {
    region       = data.aws_region.current.region
    cluster_name = var.cluster_name
    vpc_cidr     = var.network.cidr
    vpc_id       = var.network.vpc_id
    port_map     = local.forwarder_port_map
  })

  // A changed port map has to REBUILD the instance, not stop and start it:
  // cloud-init runs this script once per instance, so a restarted one keeps the
  // old DNAT chain while the security group moves to the new port. The Elastic
  // IP is a separate resource for exactly this, so the address survives.
  user_data_replace_on_change = true

  tags = merge(var.tags, {
    Name                      = "${var.name}-tunnel-forwarder"
    "dfe.hyperi.io/component" = "tunnel-forwarder"
  })

  lifecycle {
    precondition {
      condition     = contains(var.network.azs, local.forwarder_zone)
      error_message = "tunnel.address.zone is ${local.forwarder_zone == "" ? "empty and the network names no availability zone" : "'${local.forwarder_zone}', which this deployment's VPC does not span"}. Name one of ${join(", ", var.network.azs)}, or leave it empty to take the first."
    }

    // A zone with no public subnet leaves the instance placed nowhere and the
    // Elastic IP undeliverable.
    precondition {
      condition     = local.forwarder_subnet_id != ""
      error_message = "network.public_subnet_ids names no subnet in ${local.forwarder_zone}, so the forwarder's Elastic IP could never be delivered. azs and public_subnet_ids must be parallel lists."
    }
  }
}
