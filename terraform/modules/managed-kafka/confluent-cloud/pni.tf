// Freight's private handshake: Confluent's Private Network Interface, not PrivateLink.
// The ENIs live in our VPC and Confluent's account attaches them. docs/deployment/kafka/confluent-cloud.md#module-design-notes.

locals {
  pni = local.private && var.tier == "freight"

  // Interfaces the caller built are used as given, otherwise this module builds them.
  pni_supplied = length(var.private_network_interface_ids) > 0

  // Keyed by zone and index, so a zone added later renumbers nothing.
  pni_interfaces = local.pni && !local.pni_supplied ? {
    for pair in setproduct(var.network.azs, range(var.private_network_interfaces_per_zone)) :
    "${pair[0]}-${pair[1]}" => { az = pair[0], index = pair[1] }
  } : {}

  pni_interface_ids = local.pni_supplied ? var.private_network_interface_ids : [
    for key in keys(local.pni_interfaces) : aws_network_interface.pni[key].id
  ]
}

resource "confluent_gateway" "pni" {
  count = local.pni ? 1 : 0

  display_name = "${var.name}-pni"

  environment {
    id = local.environment_id
  }

  aws_private_network_interface_gateway {
    region = local.region
    zones  = local.zone_ids
  }
}

resource "aws_network_interface" "pni" {
  for_each = local.pni_interfaces

  subnet_id       = local.subnet_by_az[each.value.az]
  security_groups = [aws_security_group.private[0].id]

  description = "Confluent Freight private network interface for ${var.name} (${var.env})"

  tags = { Name = "${var.name}-confluent-${each.key}" }
}

// Lets Confluent's account attach the interface. Its account ID is read from the gateway, where it differs per gateway.
resource "aws_network_interface_permission" "pni" {
  for_each = local.pni_interfaces

  network_interface_id = aws_network_interface.pni[each.key].id
  permission           = "INSTANCE-ATTACH"
  aws_account_id       = confluent_gateway.pni[0].aws_private_network_interface_gateway[0].account
}

resource "confluent_access_point" "pni" {
  count = local.pni ? 1 : 0

  display_name = "${var.name}-pni"

  environment {
    id = local.environment_id
  }

  gateway {
    id = confluent_gateway.pni[0].id
  }

  aws_private_network_interface {
    // A set: the provider dedupes it and refuses fewer than six, so every ID must be distinct.
    network_interfaces = local.pni_interface_ids

    // This deployment's account, which owns the ENIs.
    account = data.aws_caller_identity.current[0].account_id
  }

  // The access point reads the interfaces, so the permissions land first.
  depends_on = [aws_network_interface_permission.pni]
}
