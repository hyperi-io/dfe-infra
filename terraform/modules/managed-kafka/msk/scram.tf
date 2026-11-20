// The SCRAM credential: an AmazonMSK_-named secret on a customer-managed key, as MSK requires.
// A wrong name or key fails at the association, not at create.

resource "aws_secretsmanager_secret" "scram" {
  name       = "AmazonMSK_${var.name}"
  kms_key_id = var.kms_key_arn

  description = "SCRAM credential for ${var.name} (${var.env})"

  recovery_window_in_days = var.secret_recovery_window_days
}

// Written after the cluster, so the credential path never waits on one still building.
resource "aws_secretsmanager_secret_version" "scram" {
  secret_id = aws_secretsmanager_secret.scram.id

  secret_string = jsonencode({
    username = var.scram_username
    password = var.scram_password
  })

  depends_on = [aws_msk_cluster.this]
}

// The association attaches this policy itself if absent, which shows a diff on every later plan.
data "aws_iam_policy_document" "scram" {
  statement {
    sid    = "AWSKafkaResourcePolicy"
    effect = "Allow"

    principals {
      type        = "Service"
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

  depends_on = [aws_secretsmanager_secret_version.scram]
}
