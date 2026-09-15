// The PUBLIC half of the deployment's DNS. The private zone stays in the
// cluster module: everything DFE talks to internally resolves there, inside the
// VPC only, and nothing crosses the line this module guards. The public zone
// exists solely for the handful of UIs whose users sit outside the VPC, and
// only when the caller names one.

data "aws_partition" "current" {}

resource "aws_route53_zone" "public" {
  count = var.dns.public_zone == "" ? 0 : 1

  name = var.dns.public_zone
}

// ---------------------------------------------------------------------------
// external-dns -- writes records into whichever zones exist
// ---------------------------------------------------------------------------
//
// One controller writes both zones, so its identity lives here beside the
// public zone and is handed the private one by ARN.

locals {
  zone_arns = concat(
    [var.private_zone_arn],
    aws_route53_zone.public[*].arn,
  )

  public_zone_arns = aws_route53_zone.public[*].arn
}

resource "aws_iam_role" "external_dns" {
  name               = "${var.name}-external-dns"
  assume_role_policy = var.pod_identity_trust_policy_json
}

data "aws_iam_policy_document" "external_dns" {
  statement {
    effect    = "Allow"
    actions   = ["route53:ChangeResourceRecordSets"]
    resources = local.zone_arns
  }

  // Route 53's list calls are not resource-scopable -- the API takes no zone
  // argument -- so they are granted on * and the write above is what is fenced.
  statement {
    effect    = "Allow"
    actions   = ["route53:ListHostedZones", "route53:ListResourceRecordSets", "route53:ListTagsForResource"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "external_dns" {
  name   = "${var.name}-external-dns"
  role   = aws_iam_role.external_dns.name
  policy = data.aws_iam_policy_document.external_dns.json
}

resource "aws_eks_pod_identity_association" "external_dns" {
  cluster_name = var.cluster_name

  // Namespace and service account the external-dns chart defaults to.
  namespace       = "external-dns"
  service_account = "external-dns"

  role_arn = aws_iam_role.external_dns.arn
}

// ---------------------------------------------------------------------------
// cert-manager -- Let's Encrypt DNS-01 on the public zone only
// ---------------------------------------------------------------------------
//
// Internal certificates come from the internal CA and need no cloud identity.
// This role exists only so a public UI can hold a publicly trusted certificate,
// so it is created with the public zone and not otherwise.

data "aws_iam_policy_document" "cert_manager" {
  count = var.dns.public_zone == "" ? 0 : 1

  statement {
    effect    = "Allow"
    actions   = ["route53:ChangeResourceRecordSets", "route53:ListResourceRecordSets"]
    resources = local.public_zone_arns
  }

  // A DNS-01 challenge polls the change it just submitted. Change ids are
  // account-scoped and not known in advance, so this one cannot be narrowed.
  statement {
    effect    = "Allow"
    actions   = ["route53:GetChange"]
    resources = ["arn:${data.aws_partition.current.partition}:route53:::change/*"]
  }

  // How cert-manager finds the zone for a name it has been asked to prove.
  // Takes no zone argument, so it cannot be resource-scoped.
  statement {
    effect    = "Allow"
    actions   = ["route53:ListHostedZonesByName"]
    resources = ["*"]
  }
}

resource "aws_iam_role" "cert_manager" {
  count = var.dns.public_zone == "" ? 0 : 1

  name               = "${var.name}-cert-manager"
  assume_role_policy = var.pod_identity_trust_policy_json
}

resource "aws_iam_role_policy" "cert_manager" {
  count = var.dns.public_zone == "" ? 0 : 1

  name   = "${var.name}-cert-manager"
  role   = aws_iam_role.cert_manager[0].name
  policy = data.aws_iam_policy_document.cert_manager[0].json
}

resource "aws_eks_pod_identity_association" "cert_manager" {
  count = var.dns.public_zone == "" ? 0 : 1

  cluster_name = var.cluster_name

  // Namespace and service account bootstrap.sh installs cert-manager under --
  // release cert-manager in namespace cert-manager, with no serviceAccount
  // override, so the chart names the account after the release.
  namespace       = "cert-manager"
  service_account = "cert-manager"

  role_arn = aws_iam_role.cert_manager[0].arn
}
