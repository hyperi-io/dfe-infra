// The identity the in-cluster bootstrap Job uses to write the first Kafka ACL.
// Why IAM and not an ACL: docs/deployment/kafka/aws-msk-operations.md#the-bootstrap-job----acls.

resource "aws_iam_role" "bootstrap" {
  name                 = "${var.name}-kafka-bootstrap"
  path                 = var.iam_path
  assume_role_policy   = var.pod_identity_trust_policy_json
  permissions_boundary = var.permissions_boundary
}

data "aws_iam_policy_document" "bootstrap" {
  // Connect, read the cluster and AlterCluster, which is what an ACL write needs on MSK.
  statement {
    effect = "Allow"

    actions = [
      "kafka-cluster:Connect",
      "kafka-cluster:DescribeCluster",
      "kafka-cluster:AlterCluster",
    ]

    resources = [aws_msk_cluster.this.arn]
  }

  // Nothing at topic or group scope: an ACL write is authorised as ALTER CLUSTER, and dfe-engine creates the topics.
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
