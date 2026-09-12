// AWS Load Balancer Controller -- the swap for an in-cluster load balancer.
//
// A type=LoadBalancer Service annotated aws-load-balancer-type: external is
// OWNED by this controller; without it the Service sits at "Ensuring load
// balancer" forever, because the annotation tells the in-tree controller to
// keep its hands off. Observed on this cluster before the controller existed.
//
// The policy document is AWS's published one, vendored rather than fetched at
// plan time so a plan is not hostage to a raw.githubusercontent fetch.

resource "aws_iam_policy" "lbc" {
  name   = "${var.name}-lbc"
  policy = file("${path.module}/iam/aws-load-balancer-controller.json")
}

resource "aws_iam_role" "lbc" {
  name               = "${var.name}-lbc"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

resource "aws_iam_role_policy_attachment" "lbc" {
  role       = aws_iam_role.lbc.name
  policy_arn = aws_iam_policy.lbc.arn
}

resource "aws_eks_pod_identity_association" "lbc" {
  cluster_name    = aws_eks_cluster.main.name
  namespace       = "kube-system"
  service_account = "aws-load-balancer-controller"
  role_arn        = aws_iam_role.lbc.arn
}
