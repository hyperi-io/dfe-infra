// One customer-managed key per deployment: EKS Secrets, MSK, the ClickHouse object store and
// every Karpenter node's root volume take it, so there is one key to audit, rotate and revoke.
// Stateful PVCs are the exception: the gp3 class bootstrap.sh creates uses the account's default
// EBS key (bootstrap/templates/storageclass-aws.yaml.tpl).

// lifecycle.destroy only takes a constant, so tags.lifecycle picks between two keys.
locals {
  kms_key_ephemeral = var.tags.lifecycle == "ephemeral"
  kms_description   = "${var.name} (${var.env}) -- Kubernetes secrets, Kafka and block storage"

  // KMS's minimum (it has no zero), so a rebuild under the same name is not held up.
  kms_deletion_window_days = 7

  kms_key_arn = one(concat(aws_kms_key.ephemeral[*].arn, aws_kms_key.retained[*].arn))
  kms_key_id  = one(concat(aws_kms_key.ephemeral[*].key_id, aws_kms_key.retained[*].key_id))
}

resource "aws_kms_key" "ephemeral" {
  count = local.kms_key_ephemeral ? 1 : 0

  description             = local.kms_description
  enable_key_rotation     = true
  deletion_window_in_days = local.kms_deletion_window_days
}

// A persistent deployment's object-store, CloudTrail and session-log buckets keep their objects
// through a destroy, all encrypted under this key, so destroy forgets the key rather than
// scheduling its deletion (docs/deployment/aws-teardown.md).
resource "aws_kms_key" "retained" {
  count = local.kms_key_ephemeral ? 0 : 1

  description         = local.kms_description
  enable_key_rotation = true

  lifecycle {
    destroy = false
  }
}

resource "aws_kms_alias" "this" {
  // The alias is how an operator finds the key in the console.
  name          = "alias/${var.name}"
  target_key_id = local.kms_key_id
}

// aws_kms_key_policy replaces the whole policy, so this module is the key's one policy owner: a
// sibling that needs a grant passes it in through var.key_policy_grants (CONTRACT.md), because a
// second aws_kms_key_policy would strip this one. The root statement restates AWS's default (the
// account root decides via IAM). Without it the grants would be the entire policy and every
// IAM-side grant on the key would break.
//
// jsonencode() rather than a data "aws_iam_policy_document": a mocked document's .json is a
// fixed default, so a module test could not assert on the statements.
locals {
  kms_key_policy_statements = concat(
    [
      {
        Sid       = "EnableIAMUserPermissions"
        Effect    = "Allow"
        Principal = { AWS = "arn:${data.aws_partition.current.partition}:iam::${var.provision.account}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
    ],
    [
      for grant in var.key_policy_grants : merge(
        {
          Sid       = grant.sid
          Effect    = "Allow"
          Principal = { Service = grant.principals }
          Action    = grant.actions
          Resource  = "*"
        },
        length(grant.conditions) == 0 ? {} : {
          // values stays a list even at length 1: a ternary yielding a scalar sometimes and a
          // list other times is an inconsistent-type expression tofu refuses.
          Condition = {
            for test_type in distinct([for c in grant.conditions : c.test]) :
            test_type => {
              for c in grant.conditions : c.variable => c.values
              if c.test == test_type
            }
          }
        }
      )
    ]
  )
}

resource "aws_kms_key_policy" "this" {
  key_id = local.kms_key_id

  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.kms_key_policy_statements
  })
}

// EKS asks KMS for a data key on every Secret write and a grant at cluster creation. IAM
// decides because the key policy delegates to it, and AmazonEKSClusterPolicy covers no KMS, so
// without this creation fails with "KMS key access denied".
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

    resources = [local.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "cluster_kms" {
  name   = "${var.name}-cluster-kms"
  role   = aws_iam_role.cluster.name
  policy = data.aws_iam_policy_document.cluster_kms.json
}
