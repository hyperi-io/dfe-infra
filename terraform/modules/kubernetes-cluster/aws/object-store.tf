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
//
// The association below targets the chart's dedicated clickhouse.serviceAccount
// (default dfe-clickhouse), never the release namespace's default account.
// EKS Pod Identity grants credentials to any pod matching the association's
// namespace and service account name, with no regard for which workload that
// happens to be -- when the association named "default" here, every other pod
// running as "default" in the same namespace silently inherited this role's S3
// write and delete rights alongside ClickHouse itself.

// The account id qualifies the name because S3's namespace is global and
// var.name is the dial's own metadata.name -- which deployment.example.yaml
// ships as "dfe", so an unqualified name would be a bucket anyone could have
// taken and the first apply would fail BucketAlreadyExists.
//
// A persistent deployment's ClickHouse data survives a tofu destroy, and so does
// the key it is encrypted under (kms.tf). SSE-KMS on that key adds no second key
// to audit or rotate. The EBS volumes are NOT on it: the gp3 class bootstrap.sh
// creates (bootstrap/templates/storageclass-aws.yaml.tpl) uses the account's
// default EBS key, because naming this one also means granting the
// aws-ebs-csi-driver add-on's role kms:CreateGrant over it.
//
// No versioning: a MergeTree part ClickHouse deletes itself (a merge, a TTL
// expiry) is meant to go, and a noncurrent copy would grow the bucket every merge.
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

  // The cache in front of the bucket (objectStore.cache) is disposable, so a
  // killed insert's multipart PUT is aborted rather than left growing the bucket.
  lifecycle_rules = [
    { id = "abort-incomplete-multipart-uploads", abort_multipart_days = local.multipart_abort_days },
  ]

  tags = { Name = "${var.name}-clickhouse-${var.provision.account}" }
}

// ---------------------------------------------------------------------------
// Pod Identity -- reuses data.aws_iam_policy_document.pod_identity_trust from
// eks.tf, the same trust policy every other in-cluster role in this module
// assumes.
// ---------------------------------------------------------------------------

resource "aws_iam_role" "clickhouse_object_store" {
  name                 = "${var.name}-clickhouse-object-store"
  path                 = var.iam_path
  assume_role_policy   = data.aws_iam_policy_document.pod_identity_trust.json
  permissions_boundary = var.permissions_boundary
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
      Resource = module.clickhouse_object_store.arn
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
      Resource = "${module.clickhouse_object_store.arn}/*"
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
      Resource = local.kms_key_arn
      // The same key wraps EKS Secrets and MSK's data at rest, and the key
      // policy (kms.tf) delegates to IAM, so an unconditioned grant here would
      // let this pod decrypt any ciphertext under it. ViaService confines the
      // grant to the calls S3 makes on the pod's behalf. No encryption-context
      // condition on top: bucket_key_enabled above makes S3 send the BUCKET arn
      // as the context for the bucket-level key and the object arn for
      // object-level calls, so pinning one of the two refuses the other.
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
