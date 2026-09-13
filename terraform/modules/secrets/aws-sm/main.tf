// AWS Secrets Manager as the store ESO reads, and EKS Pod Identity as how it
// reaches it. This is the swap the local path's OpenBao body describes: the
// store changes and the ClusterSecretStore's provider block changes, while
// every ExternalSecret and every reader downstream is unchanged.

data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  // Every secret this deployment owns sits under one path, which is what makes
  // the read grant below fenceable to a prefix rather than to the whole store.
  // An empty prefix drops out rather than leaving a leading slash.
  path = join("/", compact([var.prefix, var.project, var.env]))

  // "<seed>|<field>" for every field the caller left empty. Flattened because
  // random_password is one resource per value, not one per secret.
  empty_fields = toset(flatten([
    for name, fields in var.seeds : [
      for field, value in fields : "${name}|${field}" if value == ""
    ]
  ]))

  // The Kafka user's password is the CALLER's, always: a managed broker is
  // created with that value, so one generated in here could never match it.
  // Read off the seed NAMES rather than off kafka_password, because what a
  // for_each covers has to be known at plan and the password is not.
  caller_supplied = toset([
    for name, fields in var.seeds : "${name}|password"
    if startswith(name, "kafka/") && contains(keys(fields), "password")
  ])

  generated = setsubtract(local.empty_fields, local.caller_supplied)
}

// A generated value NEVER leaves this module -- it goes into the store and into
// state, and is not an output.
//
// special = false: these ride JAAS config strings, broker properties files and
// container environment variables, where punctuation needs escaping and buys no
// entropy at 32 characters. Same reasoning as the ESO Password generator's
// symbols: 0 in the Kafka user manifest.
resource "random_password" "seed" {
  for_each = local.generated

  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "seed" {
  for_each = var.seeds

  name       = "${local.path}/${each.key}"
  kms_key_id = var.kms_key_arn

  // A throwaway deployment is torn down and stood up again under the same
  // names, and Secrets Manager's default 30-day recovery window would make the
  // second create fail on a name that still exists.
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "seed" {
  for_each = var.seeds

  secret_id = aws_secretsmanager_secret.seed[each.key].id

  secret_string = jsonencode({
    for field, value in each.value :
    field => value != "" ? value : (
      contains(local.caller_supplied, "${each.key}|${field}")
      ? var.kafka_password
      : random_password.seed["${each.key}|${field}"].result
    )
  })

  // NEVER rotate on re-apply. A broker keeps the password it was given, so a
  // regenerated value would leave it authenticating against one the store no
  // longer holds and lock every DFE service out of Kafka; the UI's signing key
  // signs live session tokens, so regenerating it logs every user out. Rotation
  // is a deliberate, separate operation.
  lifecycle {
    ignore_changes = [secret_string]
  }
}

// ---------------------------------------------------------------------------
// The identity ESO reads with
// ---------------------------------------------------------------------------

resource "aws_iam_role" "eso" {
  name               = "${var.cluster_name}-external-secrets"
  assume_role_policy = var.pod_identity_trust_policy_json
}

data "aws_iam_policy_document" "eso" {
  statement {
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]

    // Secrets Manager appends six random characters to every secret's ARN, so
    // the path is matched with a trailing wildcard rather than listed.
    resources = [
      "arn:${data.aws_partition.current.partition}:secretsmanager:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:secret:${local.path}/*",
    ]
  }

  // Decrypt on the deployment's own key only. Without it every read fails,
  // because the secrets above are encrypted with a customer-managed key rather
  // than the account default.
  statement {
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "eso" {
  name   = "${var.cluster_name}-external-secrets"
  role   = aws_iam_role.eso.name
  policy = data.aws_iam_policy_document.eso.json
}

resource "aws_eks_pod_identity_association" "eso" {
  cluster_name = var.cluster_name

  // Namespace and service account bootstrap.sh installs ESO under -- release
  // external-secrets in namespace external-secrets, with no serviceAccount
  // override, so the chart names the account after the release.
  namespace       = "external-secrets"
  service_account = "external-secrets"

  role_arn = aws_iam_role.eso.arn
}
