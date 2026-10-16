// ECR pull-through cache rules, one per upstream registry the stack pulls from,
// and the containerd mirror script every node runs. Design and the rejected
// alternatives: CONTRACT.md.

data "aws_partition" "current" {}

locals {
  // An upstream ECR refuses without a credential gets no rule, and nodes pull it directly.
  active = {
    for prefix, upstream in var.upstreams : prefix => upstream
    if !upstream.requires_credential || upstream.credential_arn != ""
  }

  registry = "${var.provision.account}.dkr.ecr.${var.provision.region}.${data.aws_partition.current.dns_suffix}"

  repository_arn_prefix = "arn:${data.aws_partition.current.partition}:ecr:${var.provision.region}:${var.provision.account}:repository"

  // One ARN pattern per rule: everything ECR caches under it, and nothing else in the registry.
  cached_repository_arns = [for prefix in keys(local.active) : "${local.repository_arn_prefix}/${var.namespace}/${prefix}/*"]
}

resource "aws_ecr_pull_through_cache_rule" "this" {
  for_each = local.active

  ecr_repository_prefix = "${var.namespace}/${each.key}"
  upstream_registry_url = each.value.upstream_url
  credential_arn        = each.value.credential_arn == "" ? null : each.value.credential_arn
}

// ---------------------------------------------------------------------------
// The repositories ECR creates on a first pull
// ---------------------------------------------------------------------------

// ECR assumes this role to create a cached repository, because the template
// below names a KMS key and tags, which its own service-linked role cannot apply.
resource "aws_iam_role" "repository_creation" {
  count = length(local.active) > 0 ? 1 : 0

  name                 = "${var.name}-ecr-cache"
  path                 = var.iam_path
  permissions_boundary = var.permissions_boundary

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "sts:AssumeRole"
      // The ECR service principal. Fixed by AWS.
      Principal = { Service = "ecr.amazonaws.com" }
      Condition = { StringEquals = { "aws:SourceAccount" = var.provision.account } }
    }]
  })
}

// jsonencode rather than an aws_iam_policy_document, so a mocked plan asserts the real statements.
resource "aws_iam_role_policy" "repository_creation" {
  count = length(local.active) > 0 ? 1 : 0

  name = "${var.name}-ecr-cache"
  role = aws_iam_role.repository_creation[0].name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CreateCachedRepositories"
        Effect   = "Allow"
        Action   = ["ecr:CreateRepository", "ecr:TagResource"]
        Resource = local.cached_repository_arns
      },
      {
        // The grant ECR keeps on the key for the life of each repository it encrypts.
        Sid      = "EncryptWithTheDeploymentKey"
        Effect   = "Allow"
        Action   = ["kms:CreateGrant", "kms:RetireGrant", "kms:DescribeKey"]
        Resource = var.kms_key_arn
      },
    ]
  })
}

resource "aws_ecr_repository_creation_template" "this" {
  for_each = aws_ecr_pull_through_cache_rule.this

  prefix          = each.value.ecr_repository_prefix
  description     = "Images cached from ${var.upstreams[each.key].upstream_url} for ${var.name}"
  applied_for     = ["PULL_THROUGH_CACHE"]
  custom_role_arn = aws_iam_role.repository_creation[0].arn

  // Mutable, or ECR could never refresh a cached tag from its upstream.
  image_tag_mutability = "MUTABLE"

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }

  lifecycle_policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire an image no node has pulled for ${var.pull_expiry_days} days"
      selection = {
        tagStatus   = "any"
        countType   = "sinceImagePulled"
        countUnit   = "days"
        countNumber = var.pull_expiry_days
      }
      action = { type = "expire" }
    }]
  })

  resource_tags = var.tags

  // The role's policy has to exist before ECR first assumes the role to create a repository.
  depends_on = [aws_iam_role_policy.repository_creation]
}

// ---------------------------------------------------------------------------
// Nodes
// ---------------------------------------------------------------------------

// A pull of an uncached image is an import into a repository that may not exist yet.
resource "aws_iam_role_policy" "node_import" {
  for_each = length(local.active) > 0 ? var.node_role_arns : {}

  name = "${var.name}-ecr-cache-import"
  role = regex("[^/]+$", each.value)

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ImportThroughTheCache"
      Effect   = "Allow"
      Action   = ["ecr:BatchImportUpstreamImage", "ecr:CreateRepository"]
      Resource = local.cached_repository_arns
    }]
  })
}

locals {
  // Built from configuration alone, so the cluster module knows at plan whether a node needs a launch template.
  mirrors = [
    for prefix, upstream in local.active : {
      host = upstream.host
      // containerd's own fallback when the mirror cannot answer.
      server = "https://${upstream.upstream_url}"
      // ECR serves a cached repository at /v2/<rule prefix>/<upstream path>, which override_path tells containerd.
      endpoint = "https://${local.registry}/v2/${var.namespace}/${prefix}"
    }
  ]

  node_user_data = length(local.mirrors) == 0 ? "" : templatefile("${path.module}/templates/registry-mirrors.sh.tftpl", {
    region  = var.provision.region
    mirrors = local.mirrors
  })
}

// ---------------------------------------------------------------------------
// Teardown
// ---------------------------------------------------------------------------

// ECR creates the cached repositories, so none is in state. Left behind they
// outlive the key that encrypts them, so this deletes every one under each rule
// on destroy, after the nodes that pull through them are gone.
resource "terraform_data" "repository_teardown" {
  count = length(local.active) > 0 ? 1 : 0

  // A destroy-time command may reference only self.
  input = {
    region   = var.provision.region
    prefixes = join(" ", [for rule in aws_ecr_pull_through_cache_rule.this : rule.ecr_repository_prefix])
  }

  provisioner "local-exec" {
    when = destroy

    command = <<-EOT
      set -eu
      region="${self.output.region}"
      for prefix in ${self.output.prefixes}; do
        names="$(aws ecr describe-repositories --region "$region" --output text \
          --query "repositories[?starts_with(repositoryName, '$prefix/')].repositoryName")"
        for name in $names; do
          aws ecr delete-repository --region "$region" --repository-name "$name" --force >/dev/null
          echo "deleted cached repository $name"
        done
      done
    EOT
  }
}
