// NO NAT GATEWAY, DELIBERATELY.
//
// A NAT Gateway bills hourly PLUS per GB processed, runs whether anything uses
// it or not, and is the largest silent cost in a short-lived cluster -- more
// than the EKS control plane itself. So nodes sit in PUBLIC subnets and reach
// the internet through the internet gateway directly.
//
// That is a spike-only shape. A customer deployment puts nodes in private
// subnets behind NAT, or uses VPC endpoints.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 3)
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # the private Route 53 zone needs both to resolve

  tags = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  for_each = { for idx, az in local.azs : az => idx }

  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.key
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, each.value)
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.name}-public-${each.key}"
    # How the AWS load balancer controller discovers subnets.
    "kubernetes.io/role/elb" = "1"
  }
}

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

// Private zone, so external-dns is exercised end to end without delegating from
// the Cloudflare-hosted hyperi.io. Records resolve inside the VPC only, which is
// all the spike needs.
resource "aws_route53_zone" "private" {
  name = var.domain

  vpc {
    vpc_id = aws_vpc.this.id
  }
}
