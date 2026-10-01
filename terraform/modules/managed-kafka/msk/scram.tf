// The SCRAM credential, in the one shape MSK accepts: a Secrets Manager secret
// whose name starts with AmazonMSK_ and which is encrypted with a
// CUSTOMER-managed key. MSK refuses any other name and refuses the default
// aws/secretsmanager key, and both refusals arrive as an association failure
// rather than as a create failure, so they are easy to misread.

resource "aws_secretsmanager_secret" "scram" {
  // The AmazonMSK_ prefix is MSK's own requirement, not a convention.
  name       = "AmazonMSK_${var.name}"
  kms_key_id = var.kms_key_arn

  description = "SCRAM credential for ${var.name} (${var.env})"

  recovery_window_in_days = var.secret_recovery_window_days
}

// Written AFTER the cluster. The container has to exist first so the cluster
// can be created against a name that resolves, and the version afterwards so
// nothing in the credential path waits on a cluster that is still building.
resource "aws_secretsmanager_secret_version" "scram" {
  secret_id = aws_secretsmanager_secret.scram.id

  secret_string = jsonencode({
    username = var.scram_username
    password = var.scram_password
  })

  depends_on = [aws_msk_cluster.this]
}

// MSK reads the secret as the kafka service principal, and the association
// attaches this policy itself if it is absent -- out of band, so every
// subsequent plan shows a diff. Writing it here is what keeps the state clean.
data "aws_iam_policy_document" "scram" {
  statement {
    sid    = "AWSKafkaResourcePolicy"
    effect = "Allow"

    principals {
      type = "Service"
      // The MSK service principal. Fixed by AWS.
      identifiers = ["kafka.amazonaws.com"]
    }

    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.scram.arn]
  }
}

resource "aws_secretsmanager_secret_policy" "scram" {
  secret_arn = aws_secretsmanager_secret.scram.arn
  policy     = data.aws_iam_policy_document.scram.json
}

resource "aws_msk_scram_secret_association" "this" {
  cluster_arn     = aws_msk_cluster.this.arn
  secret_arn_list = [aws_secretsmanager_secret.scram.arn]

  // The association reads the secret, so the password has to be in it first.
  depends_on = [aws_secretsmanager_secret_version.scram]
}
