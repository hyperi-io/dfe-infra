// Freight's private handshake: Confluent's Private Network Interface.
//
// Not PrivateLink. On Freight, Confluent's brokers attach to network interfaces
// that live in OUR VPC: we create the ENIs, grant Confluent's own AWS account
// permission to attach them, and hand the IDs to an access point. Traffic never
// leaves the VPC and there is no endpoint service in the path.
//
// The count is the vendor's: 17 interfaces per subnet, 51 across three zones,
// sized so the network layer does not cap a scaling operation.

locals {
  pni = local.private && var.tier == "freight"

  // Interfaces the caller already built are used as they are; otherwise this
  // module builds them.
  pni_supplied = length(var.private_network_interface_ids) > 0

  // One ENI key per zone per index, so a zone added later does not renumber the
  // ones already built.
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
    id = confluent_environment.this.id
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

// The grant that lets Confluent's own account attach the interface. Its account
// ID is read from the gateway rather than written down -- it is Confluent's,
// not ours, and it differs per gateway.
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
    id = confluent_environment.this.id
  }

  gateway {
    id = confluent_gateway.pni[0].id
  }

  aws_private_network_interface {
    // A SET, not a list: the provider dedupes it and then refuses fewer than
    // six, so every interface here has to be a distinct ID.
    network_interfaces = local.pni_interface_ids

    // The account the ENIs live in, which is this deployment's own. Only
    // ever evaluated when local.pni is true, which is private connectivity,
    // so the data source is guaranteed to have an instance here.
    account = data.aws_caller_identity.current[0].account_id
  }

  // The access point reads the interfaces, so the permissions have to be in
  // place before it does.
  depends_on = [aws_network_interface_permission.pni]
}
