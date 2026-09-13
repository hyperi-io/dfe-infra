output "state_bucket" {
  description = "Feed to the aws root's state.bucket."
  value       = aws_s3_bucket.state.id
}

output "state_region" {
  description = "Feed to the aws root's state.region. The backend has to name the bucket's own region."
  value       = var.region
}
