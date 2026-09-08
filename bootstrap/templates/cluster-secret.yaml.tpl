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
    dfe.hyperi.io/profile: "${DFE_PROFILE}"
    # "true" only when no external git was supplied -> deploy the bundled Forgejo
    # fallback (gated in appsets/layer2-deploy-repo.yaml). External GitHub/GitLab
    # deploys set "false" and no in-cluster git server is created.
    dfe.hyperi.io/bundled-deploy-repo: "${DFE_BUNDLED_DEPLOY_REPO}"
    # external-dns provider, "none" for a deployment that publishes no records;
    # layer1-addons deploys the controller only for a value other than none.
    dfe.hyperi.io/dns-provider: "${DFE_DNS_PROVIDER}"
  annotations:
    # Identity
    dfe.hyperi.io/env: "${DFE_ENV}"
    dfe.hyperi.io/cloud: "${DFE_CLOUD}"
    dfe.hyperi.io/region: "${DFE_REGION}"
    dfe.hyperi.io/domain: "${DFE_DOMAIN}"
    dfe.hyperi.io/profile: "${DFE_PROFILE}"
    # Front-door addresses this deployment's DNS already names. Empty leaves the
    # choice to the LB pool, which is what re-rolls them on a rebuild.
    dfe.hyperi.io/gateway_address: "${DFE_GATEWAY_IP}"
    dfe.hyperi.io/receiver_address: "${DFE_RECEIVER_IP}"
    # GitOps source
    dfe.hyperi.io/repo_url: "${DFE_REPO_URL}"
    dfe.hyperi.io/target_revision: "${DFE_TARGET_REVISION}"
    # Deploy-specific gitops repo -- the SINGLE source for the deploy repo coords.
    # Argo reads it here (layer2-apps source 2 + the git generator), and the appset
    # injects the same value into the dfe-engine chart (gitops.repoUrl/branch), so
    # the engine WRITES the repo it reads -- no separate DFE_GITOPS_REPO_URL to keep
    # in sync. Unset DFE_CONFIG_REPO_URL -> the in-cluster Forgejo fallback; set it
    # to an external GitHub/GitLab/self-hosted repo for the primary path.
    dfe.hyperi.io/config_repo_url: "${DFE_CONFIG_REPO_URL}"
    dfe.hyperi.io/config_repo_revision: "${DFE_CONFIG_REPO_REVISION}"
    # Infrastructure outputs (from Terraform)
    dfe.hyperi.io/storage_class: "${DFE_STORAGE_CLASS}"
    dfe.hyperi.io/dfe_namespace: "${DFE_NAMESPACE}"
    dfe.hyperi.io/clickhouse_host: "${DFE_CLICKHOUSE_HOST}"
    dfe.hyperi.io/kafka_bootstrap: "${DFE_KAFKA_BOOTSTRAP}"
    dfe.hyperi.io/otel_endpoint: "${DFE_OTEL_ENDPOINT}"
    # Deployment-wide retention; layer2-apps and layer2-data inject it into the
    # dfe-engine and dfe-schema charts as retention.defaultTtlDays.
    dfe.hyperi.io/clickhouse_default_ttl_days: "${DFE_CLICKHOUSE_DEFAULT_TTL_DAYS}"
    # Workload identity annotations JSON (from tf-iam output)
    dfe.hyperi.io/workload_identity_annotations: '${DFE_WORKLOAD_IDENTITY_ANNOTATIONS}'
type: Opaque
stringData:
  name: "dfe-${DFE_CLOUD}-${DFE_ENV}"
  server: https://kubernetes.default.svc
  config: |
    {
      "tlsClientConfig": {"insecure": false}
    }
