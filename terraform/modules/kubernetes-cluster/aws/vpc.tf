// Nodes, brokers, databases and every private endpoint sit in PRIVATE subnets.
// Public subnets carry two things and nothing else: internet-facing load
// balancers, and the NAT gateways the private half reaches the internet
// through. DFE's images come from ghcr.io, which no VPC interface endpoint can
// reach, so egress is a requirement rather than a convenience.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  // Three zones: enough for a quorum (etcd, Keeper, Kafka controllers) without
  // paying cross-zone transfer on a fourth.
  azs = slice(data.aws_availability_zones.available.names, 0, 3)

  // Sixteen /20s in a /16. The private half takes the first three, the public
  // half the three starting at the midpoint, so the two never collide whatever
  // the caller sizes provision.cidr to.
  private_cidrs = { for i, az in local.azs : az => cidrsubnet(var.provision.cidr, 4, i) }
  public_cidrs  = { for i, az in local.azs : az => cidrsubnet(var.provision.cidr, 4, i + 8) }

  // per-az survives a zone failure and keeps egress traffic in its own zone;
  // single is one gateway every zone routes to.
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

  // No map_public_ip_on_launch: nothing is LAUNCHED here. Load balancers and
  // NAT gateways bring their own addresses, and a default-on public IP would
  // silently give any future instance one.

  tags = {
    Name = "${var.name}-public-${each.key}"
    // The two tags the AWS load balancer controller discovers an
    // internet-facing subnet by. Keys defined by EKS, so they are literal by
    // necessity; the cluster tag is what keeps discovery right when a VPC
    // holds more than one cluster.
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
    // The tag Karpenter's subnetSelectorTerms match on. It is written here
    // rather than in karpenter.tf because aws_ec2_tag against a subnet this
    // module already owns produces a tag each side removes on every plan.
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

  // A NAT gateway with no route to the internet gateway is created and then
  // fails, so the dependency is stated rather than inferred from the routes.
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

// One private route table per zone either way, so switching network.nat
// between single and per-az changes which gateway a route points at rather
// than how many tables exist.
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

// The S3 gateway endpoint is free and routes S3 traffic off the NAT gateway,
// which bills per GB. ClickHouse object storage, container layers from ECR and
// the state bucket all take that path, so it is always created.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  vpc_endpoint_type = "Gateway"

  // AWS's own naming for a regional service endpoint.
  service_name = "com.amazonaws.${var.provision.region}.s3"

  route_table_ids = [for rt in aws_route_table.private : rt.id]

  tags = { Name = "${var.name}-s3" }
}
