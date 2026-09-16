// The identity the in-cluster bootstrap Job authenticates with.
//
// Kafka ACLs live in the Kafka data plane, not the AWS control plane, so tofu
// cannot create them: it has no Kafka client and the brokers sit in private
// subnets. Once the cluster stops granting by default, nothing can create the
// FIRST ACL unless it is already permitted -- and SASL/IAM is the way out,
// because there the IAM policy IS the authorisation and no ACL has to exist.
//
// The Job itself -- the SCRAM principal's ACLs and the landing topics -- is
// chart work. This module mints what it needs and outputs the role.

locals {
  // MSK's IAM resource names. A topic and a group ARN both carry the cluster
  // name and its UUID, which is why the role can only be built after the
  // cluster is known.
  msk_arn_prefix = "arn:${data.aws_partition.current.partition}:kafka:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}"

  cluster_topics = "${local.msk_arn_prefix}:topic/${var.name}/${aws_msk_cluster.this.cluster_uuid}/*"
  cluster_groups = "${local.msk_arn_prefix}:group/${var.name}/${aws_msk_cluster.this.cluster_uuid}/*"
}

resource "aws_iam_role" "bootstrap" {
  name               = "${var.name}-kafka-bootstrap"
  assume_role_policy = var.pod_identity_trust_policy_json
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

  // Topic scope: create the landing and DLQ topics, set their partitions and
  // retention, and produce and consume for the smoke check that proves the
  // cluster works before DFE is pointed at it.
  statement {
    effect = "Allow"

    actions = [
      "kafka-cluster:CreateTopic",
      "kafka-cluster:DescribeTopic",
      "kafka-cluster:AlterTopic",
      "kafka-cluster:WriteData",
      "kafka-cluster:ReadData",
    ]

    resources = [local.cluster_topics]
  }

  // Group scope: the ACLs the Job writes fence DFE's consumers to the dfe-
  // prefix, and asserting them means reading and altering the groups they name.
  statement {
    effect = "Allow"

    actions = [
      "kafka-cluster:DescribeGroup",
      "kafka-cluster:AlterGroup",
    ]

    resources = [local.cluster_groups]
  }
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
