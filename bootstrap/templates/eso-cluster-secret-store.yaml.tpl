# ESO ClusterSecretStore — configures ESO to pull secrets from the target backend.
# For local/Rancher: OpenBao (Vault-compatible) AppRole auth.
# For cloud targets: replace provider block with aws/gcp/azure provider (per cloud.yaml values).
#
# `conditions` fences WHO may use this store to the namespaces DFE actually
# runs in -- see the aws-sm sibling template for the reasoning (no namespace
# here carries a common label, so this is the literal list the appsets place a
# chart in, plus cert-manager for the internal CA persist path and the
# deployment's own app namespace). Without it any workload that can create an
# ExternalSecret in an unrelated namespace can materialise every DFE
# credential the AppRole reaches into its own namespace.
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
    vault:
      server: "${DFE_VAULT_ADDR}"
      path: "secret"
      version: "v2"
      auth:
        appRole:
          path: approle
          roleId: "${DFE_VAULT_ROLE_ID}"
          secretRef:
            name: dfe-vault-approle-secret
            namespace: external-secrets
            key: roleSecretID
