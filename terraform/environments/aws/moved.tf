// State addresses for the edge module's move out of kubernetes-cluster/aws.
//
// Without a block here OpenTofu reads the old address as a destroy and the new
// one as a create. On aws_route53_zone.public that is not a churn cost: a
// recreated zone is handed a NEW NS set, the parent zone's delegation still
// names the old one, and every public name stops resolving until someone
// notices and re-delegates. The IAM addresses below are the same shape of
// problem in a smaller way -- a recreated role breaks the Pod Identity
// association naming it, and the controller keeps reporting Ready.
//
// A moved block belongs in the common ancestor of both addresses, which is this
// root: neither module can name the other's resources. Each block moves the
// RESOURCE rather than an instance of it, so an index shape is carried over
// unchanged -- module.cluster.aws_route53_zone.public[0] lands as
// module.edge[0].aws_route53_zone.public[0] with no re-keying.
//
// These blocks may be deleted once every deployment has applied through them.

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
