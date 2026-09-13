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
    # A LABEL copy of the cloud annotation below: an ApplicationSet cluster
    # selector's matchExpressions can only match labels, never annotations, and
    # layer1-addons.yaml gates the AWS Load Balancer Controller on this key.
    dfe.hyperi.io/cloud: "${DFE_CLOUD}"
  annotations:
    # Identity
    dfe.hyperi.io/env: "${DFE_ENV}"
    dfe.hyperi.io/cloud: "${DFE_CLOUD}"
    dfe.hyperi.io/region: "${DFE_REGION}"
    dfe.hyperi.io/domain: "${DFE_DOMAIN}"
    dfe.hyperi.io/profile: "${DFE_PROFILE}"
    # The EKS cluster's own name, for the LBC chart's subnet/tag discovery;
    # empty on non-EKS clouds, where DFE_KUBE_CLUSTER_NAME_ANNOTATION is unset
    # and this line renders blank rather than an empty-valued annotation.
    ${DFE_KUBE_CLUSTER_NAME_ANNOTATION}
    # The karpenter-pools chart's three cluster facts; each renders blank on a
    # non-AWS cloud, same as the cluster_name annotation above.
    ${DFE_KARPENTER_DISCOVERY_TAG_ANNOTATION}
    ${DFE_KARPENTER_INSTANCE_PROFILE_ANNOTATION}
    ${DFE_KARPENTER_KMS_KEY_ID_ANNOTATION}
    # Front-door addresses this deployment's DNS already names. Empty leaves the
    # choice to the LB pool, which is what re-rolls them on a rebuild.
    dfe.hyperi.io/gateway_address: "${DFE_GATEWAY_IP}"
    dfe.hyperi.io/receiver_address: "${DFE_RECEIVER_IP}"
    # GitOps source
    dfe.hyperi.io/repo_url: "${DFE_REPO_URL}"
    dfe.hyperi.io/target_revision: "${DFE_TARGET_REVISION}"
    # The certified stack version (versions.yaml), distinct from the git ref above.
    dfe.hyperi.io/stack_version: "${DFE_STACK_VERSION}"
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
    dfe.hyperi.io/kafka_provider: "${DFE_KAFKA_PROVIDER}"
    # "external" for a managed broker (msk, confluent-cloud, redpanda-cloud),
    # blank otherwise so the profile overlay's own kafka.mode is left alone --
    # the appset parameter reading this is emitted only when it is non-empty.
    dfe.hyperi.io/kafka_mode: "${DFE_KAFKA_MODE}"
    # Bare broker hosts (no port), for the otel-collector chart's MSK
    # open_monitoring scrape. Empty on every provider but msk.
    dfe.hyperi.io/kafka_broker_hosts: "${DFE_KAFKA_BROKER_HOSTS}"
    # The SASL/IAM endpoint the in-cluster MSK bootstrap Job connects to, its
    # Pod Identity role, and the broker's own SCRAM credential reference.
    # Empty on every provider but msk; only bootstrap_iam is read back today
    # (layer2-data.yaml, into kafka.external.msk.bootstrapIam).
    dfe.hyperi.io/kafka_bootstrap_iam: "${DFE_KAFKA_BOOTSTRAP_IAM}"
    dfe.hyperi.io/kafka_bootstrap_role_arn: "${DFE_KAFKA_BOOTSTRAP_ROLE_ARN}"
    dfe.hyperi.io/kafka_credential_ref: "${DFE_KAFKA_CREDENTIAL_REF}"
    dfe.hyperi.io/otel_endpoint: "${DFE_OTEL_ENDPOINT}"
    # Deployment-wide retention; layer2-apps and layer2-data inject it into the
    # dfe-engine and dfe-schema charts as retention.defaultTtlDays.
    dfe.hyperi.io/clickhouse_default_ttl_days: "${DFE_CLICKHOUSE_DEFAULT_TTL_DAYS}"
    # Workload identity annotations JSON (from tf-iam output)
    dfe.hyperi.io/workload_identity_annotations: '${DFE_WORKLOAD_IDENTITY_ANNOTATIONS}'
    # The in-cluster toolbox pod's dial facts (deployment.yaml's
    # toolbox.pod.*), read back by layer2-platform.yaml for the dfe-toolbox
    # app alone. Always present (unlike the karpenter facts above, this chart
    # is not cloud-gated), quoted here because every annotation value is a
    # string -- the appset re-emits enabled/kubeApiAccess unquoted so they
    # reach the chart as real YAML booleans, which its own validate.yaml
    # requires.
    dfe.hyperi.io/toolbox_pod_enabled: "${DFE_TOOLBOX_POD_ENABLED}"
    dfe.hyperi.io/toolbox_pod_kube_api_access: "${DFE_TOOLBOX_POD_KUBE_API_ACCESS}"
    dfe.hyperi.io/toolbox_pod_ttl_seconds: "${DFE_TOOLBOX_POD_TTL_SECONDS}"
type: Opaque
stringData:
  name: "dfe-${DFE_CLOUD}-${DFE_ENV}"
  server: https://kubernetes.default.svc
  config: |
    {
      "tlsClientConfig": {"insecure": false}
    }
