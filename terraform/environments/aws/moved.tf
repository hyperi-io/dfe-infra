// The edge module's resources moved out of kubernetes-cluster/aws (docs/deployment/aws-operations.md#admin-ui-exposure).
// Delete these once every deployment has applied through them.

moved {
  from = module.cluster.aws_iam_policy.lbc
  to   = module.edge[0].aws_iam_policy.lbc
}

moved {
  from = module.cluster.aws_iam_role.lbc
  to   = module.edge[0].aws_iam_role.lbc
}

moved {
  from = module.cluster.aws_iam_role_policy_attachment.lbc
  to   = module.edge[0].aws_iam_role_policy_attachment.lbc
}

moved {
  from = module.cluster.aws_eks_pod_identity_association.lbc
  to   = module.edge[0].aws_eks_pod_identity_association.lbc
}

moved {
  from = module.cluster.aws_route53_zone.public
  to   = module.edge[0].aws_route53_zone.public
}

moved {
  from = module.cluster.aws_iam_role.external_dns
  to   = module.edge[0].aws_iam_role.external_dns
}

moved {
  from = module.cluster.aws_iam_role_policy.external_dns
  to   = module.edge[0].aws_iam_role_policy.external_dns
}

moved {
  from = module.cluster.aws_eks_pod_identity_association.external_dns
  to   = module.edge[0].aws_eks_pod_identity_association.external_dns
}

moved {
  from = module.cluster.aws_iam_role.cert_manager
  to   = module.edge[0].aws_iam_role.cert_manager
}

moved {
  from = module.cluster.aws_iam_role_policy.cert_manager
  to   = module.edge[0].aws_iam_role_policy.cert_manager
}

moved {
  from = module.cluster.aws_eks_pod_identity_association.cert_manager
  to   = module.edge[0].aws_eks_pod_identity_association.cert_manager
}
