// The identity the in-cluster bootstrap Job authenticates with.
//
// Kafka ACLs live in the Kafka data plane, not the AWS control plane, so tofu
// cannot create them: it has no Kafka client and the brokers sit in private
// subnets. Once the cluster stops granting by default, nothing can create the
// FIRST ACL unless it is already permitted -- and SASL/IAM is the way out,
// because there the IAM policy IS the authorisation and no ACL has to exist.
//
// The Job itself is chart work: it sets the SCRAM principal's ACLs and creates
// no topic. This module mints what it needs and outputs the role.

resource "aws_iam_role" "bootstrap" {
  name                 = "${var.name}-kafka-bootstrap"
  path                 = var.iam_path
  assume_role_policy   = var.pod_identity_trust_policy_json
  permissions_boundary = var.permissions_boundary
}

data "aws_iam_policy_document" "bootstrap" {
  // Cluster scope: connect at all, read the cluster's metadata, and alter it --
  // which on MSK is the permission an ACL write needs.
  statement {
    effect = "Allow"

    actions = [
      "kafka-cluster:Connect",
      "kafka-cluster:DescribeCluster",
      "kafka-cluster:AlterCluster",
    ]

    resources = [aws_msk_cluster.this.arn]
  }

  // Nothing at topic or group scope: an ACL write, even one naming a topic or a
  // group, is authorised as ALTER CLUSTER, and dfe-engine creates the topics.
}

resource "aws_iam_role_policy" "bootstrap" {
  name   = "${var.name}-kafka-bootstrap"
  role   = aws_iam_role.bootstrap.name
  policy = data.aws_iam_policy_document.bootstrap.json
}

resource "aws_eks_pod_identity_association" "bootstrap" {
  cluster_name = var.eks_cluster_name

  namespace       = var.pod_identity.namespace
  service_account = var.pod_identity.service_account

  role_arn = aws_iam_role.bootstrap.arn
}
