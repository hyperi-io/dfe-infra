# ESO ClusterSecretStore -- AWS Secrets Manager. Rendered when
# DFE_SECRETS_BACKEND=aws-sm; the OpenBao body is the sibling template.
#
# There is deliberately no auth block: ESO falls back to the controller pod's
# own credentials when neither `jwt` nor `secretRef` is given, which is how EKS
# Pod Identity reaches it. The tofu root associates the role with the
# external-secrets service account, so nothing here carries a credential, and
# serviceAccountRef cannot be used alongside Pod Identity.
#
# `prefix` is prepended to every remoteRef with no separator added, so
# DFE_SECRETS_PREFIX_PATH already ends in a slash -- and is empty when the
# deployment keeps its secrets at the root.
#
# `conditions` fences WHO may use this store to the namespaces DFE actually
# runs in. A ClusterSecretStore with none is usable from every namespace in
# the cluster -- the IAM policy already scopes WHAT it can read
# (<prefix>/<project>/<env>/*), but with no condition here any workload that
# can create an ExternalSecret in an unrelated namespace (a compromised app, a
# tenant, a CI job with namespace-scoped RBAC) can materialise every DFE
# credential under that prefix into its own namespace. No namespace here
# carries a common label -- every one is created by ArgoCD's
# CreateNamespace=true with none -- so the fence is the literal list of
# namespaces the appsets actually place a chart in (argocd/appsets/layer2-*),
# plus cert-manager (the internal CA root's persist/restore path,
# tls.internalCA.persist) and the deployment's own app namespace. Add a
# namespace here when an appset gains one.
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: dfe-secret-store
spec:
  conditions:
    - namespaces:
        - "${DFE_NAMESPACE}"
        - argocd
        - cert-manager
        - clickhouse
        - cnpg
        - forgejo
        - kafka
        - links
        - otel
        - strimzi
  provider:
    aws:
      service: SecretsManager
      region: "${DFE_SECRETS_REGION}"
      prefix: "${DFE_SECRETS_PREFIX_PATH}"
