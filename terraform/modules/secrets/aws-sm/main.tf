// AWS Secrets Manager as the store ESO reads, and EKS Pod Identity as how it
// reaches it. This is the swap the local path's OpenBao body describes: the
// store changes and the ClusterSecretStore's provider block changes, while
// every ExternalSecret and every reader downstream is unchanged. dfe-engine has
// a second identity, which writes the secrets it mints under engine/.

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

  // The Kafka credential is the CALLER's, always: a managed broker is created
  // with that password, or its vendor mints the key and secret, so a value
  // generated in here could never match it. Read off the seed NAMES and FIELDS
  // rather than off kafka_password and kafka_username, because what a for_each
  // covers has to be known at plan and the values are not.
  kafka_supplied = toset(flatten([
    for name, fields in var.seeds : [
      for field in keys(fields) : "${name}|${field}" if contains(["password", "username"], field)
    ] if startswith(name, "kafka/")
  ]))

  // An external ClickHouse already holds its password, so the caller's copy is the only one that matches.
  clickhouse_supplied = toset([
    for field in keys(lookup(var.seeds, "clickhouse/external", {})) : "clickhouse/external|${field}" if field == "password"
  ])

  caller_supplied = setunion(local.kafka_supplied, local.clickhouse_supplied)

  generated = setsubtract(local.empty_fields, local.caller_supplied)

  // Secrets Manager appends six random characters to every secret's ARN, so a
  // path is matched with a trailing wildcard rather than listed.
  secret_arn_prefix = "arn:${data.aws_partition.current.partition}:secretsmanager:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:secret:${local.path}"

  // dfe-engine writes what it mints under this, and var.seeds refuses a name inside it.
  engine_name_prefix = "${local.path}/engine"
  engine_arn_prefix  = "${local.secret_arn_prefix}/engine"

  // The engine's thin chart names its service account after the component (scalo-service).
  engine_service_account = "dfe-engine"
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
  // second create fail on a name that still exists -- var.recovery_window_days
  // is 0 for exactly that case, and the vendor default otherwise.
  recovery_window_in_days = var.recovery_window_days
}

resource "aws_secretsmanager_secret_version" "seed" {
  for_each = var.seeds

  secret_id = aws_secretsmanager_secret.seed[each.key].id

  secret_string = jsonencode({
    for field, value in each.value :
    field => value != "" ? value : (
      contains(local.clickhouse_supplied, "${each.key}|${field}") ? var.clickhouse_password : (
        contains(local.kafka_supplied, "${each.key}|${field}")
        ? (field == "username" ? var.kafka_username : var.kafka_password)
        : random_password.seed["${each.key}|${field}"].result
      )
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
  name                 = "${var.cluster_name}-external-secrets"
  path                 = var.iam_path
  assume_role_policy   = var.pod_identity_trust_policy_json
  permissions_boundary = var.permissions_boundary
}

data "aws_iam_policy_document" "eso" {
  statement {
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = ["${local.secret_arn_prefix}/*"]
  }

  // The engine alone reads what it mints, so no ExternalSecret can copy its signing key or a tenant password out.
  statement {
    effect    = "Deny"
    actions   = ["secretsmanager:*"]
    resources = ["${local.engine_arn_prefix}/*"]
  }

  // A secret whose name a service fixes outside the path, such as MSK's AmazonMSK_ one, read where it lives.
  dynamic "statement" {
    for_each = length(var.read_secret_arns) > 0 ? [1] : []

    content {
      effect    = "Allow"
      actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      resources = var.read_secret_arns
    }
  }

  // The gateway chart's PushSecret creates the internal CA root under pki/, tagged managed-by, and nothing else in the store is writable.
  statement {
    effect = "Allow"
    actions = [
      "secretsmanager:CreateSecret",
      "secretsmanager:PutSecretValue",
      "secretsmanager:TagResource",
      "secretsmanager:DescribeSecret",
    ]
    resources = ["${local.secret_arn_prefix}/pki/*"]
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

// ---------------------------------------------------------------------------
// The identity dfe-engine writes what it mints with
// ---------------------------------------------------------------------------

resource "aws_iam_role" "engine" {
  name                 = "${var.cluster_name}-engine-secrets"
  path                 = var.iam_path
  assume_role_policy   = var.pod_identity_trust_policy_json
  permissions_boundary = var.permissions_boundary
}

data "aws_iam_policy_document" "engine" {
  // The engine names every secret it reads or writes, so it lists nothing.
  statement {
    effect = "Allow"
    actions = [
      "secretsmanager:CreateSecret",
      "secretsmanager:UpdateSecret",
      "secretsmanager:GetSecretValue",
      "secretsmanager:DeleteSecret",
      "secretsmanager:TagResource",
    ]
    resources = ["${local.engine_arn_prefix}/*"]
  }

  // The same key wraps EKS Secrets and MSK, so it is used only through Secrets Manager, and only for its own prefix.
  statement {
    effect    = "Allow"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.kms_key_arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${data.aws_region.current.region}.${data.aws_partition.current.dns_suffix}"]
    }

    // Secrets Manager puts the secret's full ARN in the encryption context.
    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:SecretARN"
      values   = ["${local.engine_arn_prefix}/*"]
    }
  }
}

resource "aws_iam_role_policy" "engine" {
  name   = "${var.cluster_name}-engine-secrets"
  role   = aws_iam_role.engine.name
  policy = data.aws_iam_policy_document.engine.json
}

resource "aws_eks_pod_identity_association" "engine" {
  cluster_name = var.cluster_name

  namespace       = var.engine_namespace
  service_account = local.engine_service_account

  role_arn = aws_iam_role.engine.arn
}
