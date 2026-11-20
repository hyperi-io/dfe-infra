// The Pod Identity role the otel collector's provider-logs workload reads the EKS control-plane and CloudTrail log groups through. Built under sink = otel only.

locals {
  provider_logs_enabled = var.telemetry.sink == "otel"

  // A source that is off (no stream enabled, or CloudTrail disabled) contributes none.
  provider_logs_group_names = compact([
    module.cluster.audit_log_group,
    join("", aws_cloudwatch_log_group.cloudtrail[*].name),
  ])
  provider_logs_count = local.provider_logs_enabled && length(local.provider_logs_group_names) > 0 ? 1 : 0

  // Contract names set by argocd/appsets/layer2-data.yaml and the otel-collector chart, not tunables.
  provider_logs_namespace       = "otel"
  provider_logs_service_account = "dfe-otel-collector-provider-logs"

  provider_logs_group_arns = flatten([
    for name in local.provider_logs_group_names : [
      "arn:${data.aws_partition.current.partition}:logs:${var.provision.region}:${data.aws_caller_identity.current.account_id}:log-group:${name}",
      "arn:${data.aws_partition.current.partition}:logs:${var.provision.region}:${data.aws_caller_identity.current.account_id}:log-group:${name}:*",
    ]
  ])
}

resource "aws_iam_role" "provider_logs" {
  count = local.provider_logs_count

  name = "${var.name}-provider-logs"
  path = var.iam_path

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = ["sts:AssumeRole", "sts:TagSession"]
      Principal = { Service = "pods.eks.amazonaws.com" }
    }]
  })
  permissions_boundary = var.permissions_boundary
}

// The log groups carry no KMS key, so no key grant is needed.
resource "aws_iam_role_policy" "provider_logs" {
  count = local.provider_logs_count

  name = "${var.name}-provider-logs"
  role = aws_iam_role.provider_logs[0].name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadTheProviderLogGroups"
      Effect   = "Allow"
      Action   = ["logs:FilterLogEvents", "logs:DescribeLogGroups", "logs:DescribeLogStreams"]
      Resource = local.provider_logs_group_arns
    }]
  })
}

resource "aws_eks_pod_identity_association" "provider_logs" {
  count = local.provider_logs_count

  cluster_name    = module.cluster.cluster_name
  namespace       = local.provider_logs_namespace
  service_account = local.provider_logs_service_account
  role_arn        = aws_iam_role.provider_logs[0].arn
}
