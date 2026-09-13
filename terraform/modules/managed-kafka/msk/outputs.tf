// No zookeeper output: this is a KRaft cluster and MSK returns nothing for it.

output "bootstrap" {
  description = "The SASL/SCRAM bootstrap string DFE connects with. MSK already returns bare host:port pairs, comma separated, which is the spelling the contract asks for."
  value       = aws_msk_cluster.this.bootstrap_brokers_sasl_scram
}

output "bootstrap_iam" {
  description = "The SASL/IAM bootstrap string on port 9098. The bootstrap Job uses it; nothing in DFE does."
  value       = aws_msk_cluster.this.bootstrap_brokers_sasl_iam
}

output "auth_type" {
  description = "What the chart's external.auth.type takes unchanged. IAM is enabled on the cluster too, but only the Job speaks it."
  value       = "scram"
}

output "credential_ref" {
  description = "Where the SCRAM credential lives -- a reference, never a value. The secret itself holds the username and password MSK authenticates against."
  value       = aws_secretsmanager_secret.scram.arn
}

output "network_attachment" {
  description = "The cloud side of the connection: the security group the broker interfaces carry, and the subnets they live in. A caller that has to route to the brokers reads this rather than rediscovering the network."
  value = {
    security_group_id = aws_security_group.brokers.id
    subnet_ids        = var.network.private_subnet_ids
  }
}

output "cluster_arn" {
  description = "The MSK cluster ARN, for the IAM policies and the CloudWatch alarms a caller adds outside this module."
  value       = aws_msk_cluster.this.arn
}

output "bootstrap_role_arn" {
  description = "The IAM role the in-cluster bootstrap Job assumes through Pod Identity. The chart renders the Job; this is the identity it runs as."
  value       = aws_iam_role.bootstrap.arn
}

output "autoscaler_alarm_arn" {
  description = "The broker-count scale-out alarm's ARN, for a dashboard or composite alarm a caller adds outside this module. Empty when var.autoscaling.enabled is false."
  value       = join("", aws_cloudwatch_metric_alarm.broker_scale_out[*].arn)
}

output "autoscaler_function_arn" {
  description = "The Lambda that calls UpdateBrokerCount when the scale-out alarm fires. Empty when var.autoscaling.enabled is false."
  value       = join("", aws_lambda_function.broker_scaler[*].arn)
}

output "broker_log_bucket" {
  description = "The S3 bucket broker logs land in under telemetry.sink = otel, for the fetcher's object-store source to read. Empty under telemetry.sink = cloudwatch, where the logs go to aws_cloudwatch_log_group.brokers instead."
  value       = join("", aws_s3_bucket.broker_logs[*].bucket)
}
