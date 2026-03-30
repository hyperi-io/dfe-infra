# Rendered by bootstrap.sh via envsubst. All required vars must be set before running.
# This secret registers the cluster with ArgoCD AND carries all Terraform outputs
# as dfe.hyperi.io/* annotations, which ApplicationSets read via {{ .metadata.annotations.* }}.
apiVersion: v1
kind: Secret
metadata:
  name: dfe-cluster
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: cluster
    dfe.hyperi.io/managed: "true"
  annotations:
    # Identity
    dfe.hyperi.io/env: "${DFE_ENV}"
    dfe.hyperi.io/cloud: "${DFE_CLOUD}"
    dfe.hyperi.io/region: "${DFE_REGION}"
    dfe.hyperi.io/domain: "${DFE_DOMAIN}"
    dfe.hyperi.io/tenancy: "${DFE_TENANCY}"
    # GitOps source
    dfe.hyperi.io/repo_url: "${DFE_REPO_URL}"
    dfe.hyperi.io/target_revision: "${DFE_TARGET_REVISION}"
    # Infrastructure outputs (from Terraform)
    dfe.hyperi.io/storage_class: "${DFE_STORAGE_CLASS}"
    dfe.hyperi.io/dfe_namespace: "${DFE_NAMESPACE}"
    dfe.hyperi.io/clickhouse_host: "${DFE_CLICKHOUSE_HOST}"
    dfe.hyperi.io/kafka_bootstrap: "${DFE_KAFKA_BOOTSTRAP}"
    dfe.hyperi.io/otel_endpoint: "${DFE_OTEL_ENDPOINT}"
    # Workload identity annotations JSON (from tf-iam output)
    dfe.hyperi.io/workload_identity_annotations: "${DFE_WORKLOAD_IDENTITY_ANNOTATIONS}"
type: Opaque
stringData:
  name: "dfe-${DFE_CLOUD}-${DFE_ENV}"
  server: https://kubernetes.default.svc
  config: |
    {
      "tlsClientConfig": {"insecure": false}
    }
