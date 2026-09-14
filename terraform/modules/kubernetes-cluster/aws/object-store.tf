// The S3 bucket ClickHouse's cached-object storage model writes its bulk parts
// to, and the Pod Identity role that lets the server read and write it without
// a static key. helm/charts/clickhouse-cluster's storageModel derivation
// (templates/_storage.tpl) turns cached-object on the moment
// clickhouse.objectStore.endpoint is non-empty -- until this file, nothing
// created the bucket that endpoint has to name, so a deploy that resolved
// cached-object had no object store to point at.
//
// use_environment_credentials in the chart's S3 disk config means the AWS SDK's
// own default credential chain: environment variables first, then the EKS Pod
// Identity Agent's injected container-credentials endpoint. A static key in the
// pod's environment would shadow the Pod Identity credential outright, which is
// why the chart's objectStoreEnv is gated off for this path (values.yaml,
// clickhouse.objectStore.usePodIdentity) -- the role below is the only
// credential source ClickHouse ever sees against this bucket.

resource "aws_s3_bucket" "clickhouse_object_store" {
  bucket = "${var.name}-clickhouse"

  // Same rule as the KMS deletion window above and the root's CloudTrail
  // bucket: only an ephemeral deployment gets the fast, no-confirmation
  // teardown. A persistent deployment's ClickHouse data survives a tofu
  // destroy of everything else that shares its lifecycle tag.
  force_destroy = var.tags.lifecycle == "ephemeral"

  tags = { Name = "${var.name}-clickhouse" }
}

resource "aws_s3_bucket_public_access_block" "clickhouse_object_store" {
  bucket = aws_s3_bucket.clickhouse_object_store.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

// SSE-KMS on the deployment's own key -- the same key EKS secrets, MSK and the
// EBS volumes behind every stateful pod already use, so ClickHouse's object
// data adds no second key for the customer to audit or rotate.
resource "aws_s3_bucket_server_side_encryption_configuration" "clickhouse_object_store" {
  bucket = aws_s3_bucket.clickhouse_object_store.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.this.arn
    }

    bucket_key_enabled = true
  }
}

// No versioning: unlike CloudTrail's audit trail, a MergeTree part deleted by
// ClickHouse itself (a merge, a TTL expiry) is meant to go -- keeping the
// noncurrent version around would grow the bucket for every merge cycle rather
// than protecting anything the engine did not already choose to remove.

// The cache in front of this bucket (objectStore.cache) is disposable by
// design -- the bucket is what has to survive a failed multipart PUT, not the
// upload itself, so ClickHouse retrying an insert cannot leave the bucket
// silently growing with parts nothing will ever complete.
resource "aws_s3_bucket_lifecycle_configuration" "clickhouse_object_store" {
  bucket = aws_s3_bucket.clickhouse_object_store.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

// ---------------------------------------------------------------------------
// Pod Identity -- reuses data.aws_iam_policy_document.pod_identity_trust from
// eks.tf, the same trust policy every other in-cluster role in this module
// assumes.
// ---------------------------------------------------------------------------

resource "aws_iam_role" "clickhouse_object_store" {
  name               = "${var.name}-clickhouse-object-store"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

// Built with jsonencode() directly rather than a data "aws_iam_policy_document"
// -- the same choice kms.tf explains: a mocked aws_iam_policy_document's .json
// always returns the mock's fixed default regardless of the statement blocks,
// so a module test could not assert this policy names only this bucket. The
// key policy (kms.tf) delegates to IAM via its account-root statement, so no
// key_policy_grants entry is needed here -- this role's own policy is what
// grants it, the same shape as cluster_kms in kms.tf.
locals {
  clickhouse_object_store_policy_statements = [
    {
      // The S3 disk lists what it already has on startup and before every
      // write that might collide with an existing part.
      Sid      = "ListBucket"
      Effect   = "Allow"
      Action   = "s3:ListBucket"
      Resource = aws_s3_bucket.clickhouse_object_store.arn
    },
    {
      // Read and write parts, and clean up a multipart upload the server
      // itself abandoned (a killed insert) rather than waiting on the
      // lifecycle rule above.
      Sid    = "ReadWriteObjects"
      Effect = "Allow"
      Action = [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
      ]
      Resource = "${aws_s3_bucket.clickhouse_object_store.arn}/*"
    },
    {
      // GenerateDataKey and Decrypt are what S3 itself requires of the
      // calling principal for an SSE-KMS PutObject and GetObject
      // respectively; DescribeKey is what the AWS SDK's credential/key
      // resolution calls before either.
      Sid    = "UseTheDeploymentKey"
      Effect = "Allow"
      Action = [
        "kms:GenerateDataKey",
        "kms:Decrypt",
        "kms:DescribeKey",
      ]
      Resource = aws_kms_key.this.arn
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
