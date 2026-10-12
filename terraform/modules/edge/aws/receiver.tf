// One Elastic IP per public subnet for the receiver NLB, allocated ahead of it because an NLB takes Elastic IPs at creation only (variables.tf, receiver).

locals {
  // Keyed by zone, so the allocation in one zone survives a change to another.
  receiver_subnets = var.receiver.eips ? {
    for index, zone in var.network.azs : zone => var.network.public_subnet_ids[index]
  } : {}
}

resource "aws_eip" "receiver" {
  for_each = local.receiver_subnets

  domain = "vpc"

  tags = merge(var.tags, { Name = "${var.name}-receiver-${each.key}" })
}
