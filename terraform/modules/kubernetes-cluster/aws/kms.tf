// ONE customer-managed key per deployment, not one per service. EKS envelope-
// encrypts Kubernetes Secrets with it today; MSK's data at rest, the EBS
// volumes behind the stateful pods and every node Karpenter boots (its
// EC2NodeClass encrypts the root volume with the same key) take it too, so a
// customer has one key to audit, one to rotate and one to revoke.

resource "aws_kms_key" "this" {
  description         = "${var.name} (${var.env}) -- Kubernetes secrets, Kafka and block storage"
  enable_key_rotation = true

  // KMS's own floor is 7 days; there is no zero. An ephemeral deployment
  // (tags.lifecycle) gets that floor so a rebuild under the same name is not
  // gated on a manual wait; anything else keeps the vendor's own 30-day
  // default so destroying a real deployment needs a deliberate confirmation.
  deletion_window_in_days = var.tags.lifecycle == "ephemeral" ? 7 : 30
}

resource "aws_kms_alias" "this" {
  // The alias is how an operator finds the key in the console; the key id is
  // a UUID that tells nobody anything.
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.this.key_id
}

// aws_kms_key_policy REPLACES the key's whole policy, so this module is the
// key's ONE policy owner: a sibling module that needs a grant on it (MSK's
// broker-log delivery, the root's CloudTrail bucket) hands its statement in
// through var.key_policy_grants rather than writing its own aws_kms_key_policy
// -- a second aws_kms_key_policy targeting this key would silently strip
// whatever the first one wrote, which is what managed-kafka/msk's own copy did
// before this fix. The root statement restates AWS's own default (the account
// root decides, via IAM) -- leaving it out would make the grants below the
// ENTIRE policy and break every aws_iam_role_policy that already delegates to
// IAM (the EKS cluster role below, ESO's role, ...).
//
// Built with jsonencode() directly rather than a data "aws_iam_policy_document"
// so a module test can assert on the real statement content under
// mock_provider -- a mocked aws_iam_policy_document's .json is a fixed
// default, not a computation from the statement blocks, which is also why
// managed-kafka/msk's own key-policy resource (now removed) used the same
// jsonencode() shape.
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
          // values stays a list even at length 1 -- AWS accepts a single-
          // element list exactly where it accepts a scalar, and a ternary
          // collapsing it to a scalar sometimes and a list other times is an
          // inconsistent-type expression tofu refuses to evaluate.
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
  key_id = aws_kms_key.this.id

  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.kms_key_policy_statements
  })
}

// EKS asks KMS for a data key on every Secret write and a grant at cluster
// creation. IAM decides because the key policy above delegates to it, and
// nothing in AmazonEKSClusterPolicy covers KMS, so without this the cluster
// fails to create with "KMS key access denied".
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
