// Nodes, brokers, databases and private endpoints sit in PRIVATE subnets. Public subnets hold
// only internet-facing load balancers and the NAT gateways. Egress is required: DFE's images
// come from ghcr.io, which no VPC interface endpoint reaches.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.network.az_count)

  // 2^subnet_newbits equal blocks. Private takes the first az_count and public az_count from the
  // midpoint, and the network validation holds az_count to half the block count so they never
  // collide.
  public_subnet_offset = pow(2, var.network.subnet_newbits - 1)
  private_cidrs        = { for i, az in local.azs : az => cidrsubnet(var.provision.cidr, var.network.subnet_newbits, i) }
  public_cidrs         = { for i, az in local.azs : az => cidrsubnet(var.provision.cidr, var.network.subnet_newbits, i + local.public_subnet_offset) }

  nat_azs = var.network.nat == "per-az" ? local.azs : [local.azs[0]]
}

resource "aws_vpc" "this" {
  cidr_block           = var.provision.cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # the private Route 53 zone needs both to resolve

  tags = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = var.name }
}

// ---------------------------------------------------------------------------
// Subnets
// ---------------------------------------------------------------------------

resource "aws_subnet" "public" {
  for_each = local.public_cidrs

  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = each.value

  // No map_public_ip_on_launch: nothing is launched here, and a default-on public IP would silently give a future instance one.

  tags = {
    Name = "${var.name}-public-${each.key}"
    // The tags the AWS load balancer controller discovers an internet-facing subnet by. EKS defines
    // the keys, and the cluster tag keeps discovery right when a VPC holds more than one cluster.
    "kubernetes.io/role/elb"            = "1"
    "kubernetes.io/cluster/${var.name}" = "shared"
  }
}

resource "aws_subnet" "private" {
  for_each = local.private_cidrs

  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = each.value

  tags = {
    Name                                = "${var.name}-private-${each.key}"
    "kubernetes.io/role/internal-elb"   = "1"
    "kubernetes.io/cluster/${var.name}" = "shared"
    // Karpenter's subnetSelectorTerms tag. Written here because aws_ec2_tag against a subnet this
    // module owns makes each side remove the other's tag on every plan.
    "karpenter.sh/discovery" = var.name
  }
}

// ---------------------------------------------------------------------------
// Egress
// ---------------------------------------------------------------------------

resource "aws_eip" "nat" {
  for_each = toset(local.nat_azs)

  domain = "vpc"

  tags = { Name = "${var.name}-nat-${each.key}" }
}

resource "aws_nat_gateway" "this" {
  for_each = toset(local.nat_azs)

  allocation_id = aws_eip.nat[each.key].id
  subnet_id     = aws_subnet.public[each.key].id

  tags = { Name = "${var.name}-${each.key}" }

  // A NAT gateway with no route to the internet gateway is created and then fails.
  depends_on = [aws_internet_gateway.this]
}

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

// One private route table per zone either way, so switching network.nat re-points a route
// instead of changing the table count.
resource "aws_route_table" "private" {
  for_each = toset(local.azs)

  vpc_id = aws_vpc.this.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = var.network.nat == "per-az" ? aws_nat_gateway.this[each.key].id : aws_nat_gateway.this[local.azs[0]].id
  }

  tags = { Name = "${var.name}-private-${each.key}" }
}

resource "aws_route_table_association" "private" {
  for_each = aws_subnet.private

  subnet_id      = each.value.id
  route_table_id = aws_route_table.private[each.key].id
}

// The S3 gateway endpoint is free and keeps S3 traffic off the per-GB NAT gateway, so it is always created.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  vpc_endpoint_type = "Gateway"

  // AWS's own naming for a regional service endpoint.
  service_name = "com.amazonaws.${var.provision.region}.s3"

  route_table_ids = [for rt in aws_route_table.private : rt.id]

  tags = { Name = "${var.name}-s3" }
}
