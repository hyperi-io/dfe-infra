// The S3 bucket ClickHouse's cached-object storage model writes bulk parts to, and the Pod
// Identity role that reads and writes it with no static key (docs/deployment/storage.md).
//
// A static key in the pod's environment would shadow the Pod Identity credential, which is why
// the chart's objectStoreEnv is gated off for this path (values.yaml,
// clickhouse.objectStore.usePodIdentity).
//
// The association targets the chart's dedicated clickhouse.serviceAccount, never the namespace's
// default account: Pod Identity grants to any pod matching the namespace and service account, so
// "default" would hand this role's S3 write and delete rights to every other pod there.

// The account id qualifies the name because S3's namespace is global and var.name is the dial's
// metadata.name, which deployment.example.yaml ships as "dfe": an unqualified name would fail
// BucketAlreadyExists on the first apply.
//
// SSE-KMS on the deployment key (kms.tf) adds no second key to audit or rotate. The EBS volumes
// are not on it: naming it would also mean granting the aws-ebs-csi-driver role kms:CreateGrant.
//
// No versioning: a MergeTree part ClickHouse deletes itself (a merge, a TTL expiry) is meant to
// go, and a noncurrent copy would grow the bucket on every merge.
locals {
  clickhouse_object_store_name = "${var.s3_bucket_prefix}${var.name}-clickhouse-${var.provision.account}"

  // A part this old belongs to an insert ClickHouse has long since retried or abandoned, never to data it keeps.
  multipart_abort_days = 7
}

module "clickhouse_object_store" {
  source = "../../s3-bucket"

  name        = local.clickhouse_object_store_name
  retain      = !local.kms_key_ephemeral
  kms_key_arn = local.kms_key_arn
  versioning  = false

  // The objectStore.cache in front of the bucket is disposable, so a killed insert's multipart upload is aborted, not left growing the bucket.
  lifecycle_rules = [
    { id = "abort-incomplete-multipart-uploads", abort_multipart_days = local.multipart_abort_days },
  ]

  tags = { Name = "${var.name}-clickhouse-${var.provision.account}" }
}

// ---------------------------------------------------------------------------
// Pod Identity -- reuses pod_identity_trust from eks.tf.
// ---------------------------------------------------------------------------

resource "aws_iam_role" "clickhouse_object_store" {
  name                 = "${var.name}-clickhouse-object-store"
  path                 = var.iam_path
  assume_role_policy   = data.aws_iam_policy_document.pod_identity_trust.json
  permissions_boundary = var.permissions_boundary
}

// jsonencode(), as kms.tf explains, so a module test can assert this policy names only this
// bucket. The key policy delegates to IAM through its account-root statement, so no
// key_policy_grants entry is needed.
locals {
  clickhouse_object_store_policy_statements = [
    {
      // The S3 disk lists what it has on startup and before a write that might collide with an existing part.
      Sid      = "ListBucket"
      Effect   = "Allow"
      Action   = "s3:ListBucket"
      Resource = module.clickhouse_object_store.arn
    },
    {
      // Read and write parts, and abort a multipart upload the server itself abandoned (a killed insert) without waiting on the lifecycle rule.
      Sid    = "ReadWriteObjects"
      Effect = "Allow"
      Action = [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
      ]
      Resource = "${module.clickhouse_object_store.arn}/*"
    },
    {
      // What S3 requires of the caller for an SSE-KMS PutObject and GetObject, plus DescribeKey, which the SDK calls first.
      Sid    = "UseTheDeploymentKey"
      Effect = "Allow"
      Action = [
        "kms:GenerateDataKey",
        "kms:Decrypt",
        "kms:DescribeKey",
      ]
      Resource = local.kms_key_arn
      // The key also wraps EKS Secrets and MSK and its policy delegates to IAM, so an unconditioned
      // grant would decrypt any ciphertext under it. ViaService confines it to S3's calls on the
      // pod's behalf. No encryption-context condition: bucket_key_enabled makes S3 send the bucket
      // ARN for bucket-level and the object ARN for object-level calls, so pinning one refuses the
      // other.
      Condition = {
        StringEquals = {
          "kms:ViaService" = "s3.${var.provision.region}.amazonaws.com"
        }
      }
    },
  ]
}

resource "aws_iam_role_policy" "clickhouse_object_store" {
  name = "${var.name}-clickhouse-object-store"
  role = aws_iam_role.clickhouse_object_store.name

  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.clickhouse_object_store_policy_statements
  })
}

resource "aws_eks_pod_identity_association" "clickhouse_object_store" {
  cluster_name = aws_eks_cluster.this.name

  namespace       = var.clickhouse_object_store_namespace
  service_account = var.clickhouse_object_store_service_account

  role_arn = aws_iam_role.clickhouse_object_store.arn
}
