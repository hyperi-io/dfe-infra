// AWS Load Balancer Controller -- the swap for an in-cluster load balancer.
//
// A type=LoadBalancer Service annotated aws-load-balancer-type: external is
// OWNED by this controller; without it the Service sits at "Ensuring load
// balancer" forever, because the annotation tells the in-tree controller to
// keep its hands off. Observed on the spike cluster before the controller
// existed.
//
// It is edge because no cloud door exists without it: every public listener
// this module renders is a Service this controller reconciles.
//
// The policy document is AWS's published one, vendored rather than fetched at
// plan time so a plan is not hostage to a raw.githubusercontent fetch.

resource "aws_iam_policy" "lbc" {
  name   = "${var.name}-lbc"
  path   = var.iam_path
  policy = file("${path.module}/iam/aws-load-balancer-controller.json")
}

resource "aws_iam_role" "lbc" {
  name                 = "${var.name}-lbc"
  path                 = var.iam_path
  assume_role_policy   = var.pod_identity_trust_policy_json
  permissions_boundary = var.permissions_boundary
}

resource "aws_iam_role_policy_attachment" "lbc" {
  role       = aws_iam_role.lbc.name
  policy_arn = aws_iam_policy.lbc.arn
}

resource "aws_eks_pod_identity_association" "lbc" {
  cluster_name = var.cluster_name

  // Namespace and service account the controller's own chart defaults to.
  namespace       = "kube-system"
  service_account = "aws-load-balancer-controller"

  role_arn = aws_iam_role.lbc.arn
}
