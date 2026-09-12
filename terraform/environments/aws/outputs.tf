output "cluster_name" {
  description = "Feed to: aws eks update-kubeconfig --name <this> --region <region>"
  value       = aws_eks_cluster.main.name
}

output "cluster_endpoint" {
  value = aws_eks_cluster.main.endpoint
}

output "region" {
  value = var.region
}

output "private_zone_id" {
  description = "Route 53 zone external-dns writes into (--zone-id-filter)."
  value       = aws_route53_zone.private.zone_id
}

output "domain" {
  value = var.domain
}

output "vpc_id" {
  value = aws_vpc.this.id
}

// What this costs per hour while it exists, so spend can be tracked by elapsed
// time. Cost Explorer lags up to 24h and cannot answer "what have I spent so
// far" during a run this short.
output "estimated_hourly_usd" {
  description = "On-demand-equivalent upper bound; spot is cheaper and varies."
  value       = "~0.35/hr -- EKS control plane 0.10, ${var.node_desired_size}x ${var.node_instance_types[0]} spot ~0.17, EBS gp3 ~0.05, Route53 zone ~0.001. NO NAT gateway."
}
