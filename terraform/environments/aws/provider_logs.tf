// The Pod Identity role the collector's provider-logs workload reads CloudWatch
// Logs through (helm/charts/otel-collector, clusterTelemetry.providerLogs). It
// reads the EKS control-plane log group and CloudTrail's, and nothing else.
// Under sink = cloudwatch DFE reads neither into its own database, so nothing is
// built.

locals {
  provider_logs_enabled = var.telemetry.sink == "otel"

  // Empty when a source is off: no stream enabled on the cluster, or CloudTrail disabled.
  provider_logs_group_names = compact([
    module.cluster.audit_log_group,
    join("", aws_cloudwatch_log_group.cloudtrail[*].name),
  ])
  provider_logs_count = local.provider_logs_enabled && length(local.provider_logs_group_names) > 0 ? 1 : 0

  // Where the chart puts the workload: argocd/appsets/layer2-data.yaml pins the
  // otel-collector's namespace, and the chart names the account
  // <project>-<component>-provider-logs. Both are contract names, not tunables.
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

  // The EKS Pod Identity service principal. Fixed by AWS.
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

// Read only, and only those groups. The log groups carry no KMS key, so no key grant is needed.
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
