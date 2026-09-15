// One zone here, and the split is the private-by-default principle in DNS form.
// Everything DFE talks to internally resolves in this PRIVATE zone, inside the
// VPC only. The public zone, external-dns's identity and cert-manager's DNS-01
// identity all belong to the edge module (terraform/modules/edge/aws/dns.tf),
// because they exist only so something outside the VPC can reach in.

resource "aws_route53_zone" "private" {
  name = var.dns.private_zone

  vpc {
    vpc_id = aws_vpc.this.id
  }
}
