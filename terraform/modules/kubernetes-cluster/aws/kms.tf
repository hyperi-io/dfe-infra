// ONE customer-managed key per deployment, not one per service. EKS envelope-
// encrypts Kubernetes Secrets with it today; MSK's data at rest and the EBS
// volumes behind the stateful pods take the same key, so a customer has one key
// to audit, one to rotate and one to revoke.

resource "aws_kms_key" "this" {
  description             = "${var.name} (${var.env}) -- Kubernetes secrets, Kafka and block storage"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "this" {
  // The alias is how an operator finds the key in the console; the key id is
  // a UUID that tells nobody anything.
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.this.key_id
}

// EKS asks KMS for a data key on every Secret write and a grant at cluster
// creation. The default key policy hands authority to the account root, which
// means IAM decides -- and nothing in AmazonEKSClusterPolicy covers KMS, so
// without this the cluster fails to create with "KMS key access denied".
data "aws_iam_policy_document" "cluster_kms" {
  statement {
    effect = "Allow"

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ListGrants",
      "kms:DescribeKey",
      "kms:CreateGrant",
    ]

    resources = [aws_kms_key.this.arn]
  }
}

resource "aws_iam_role_policy" "cluster_kms" {
  name   = "${var.name}-cluster-kms"
  role   = aws_iam_role.cluster.name
  policy = data.aws_iam_policy_document.cluster_kms.json
}
