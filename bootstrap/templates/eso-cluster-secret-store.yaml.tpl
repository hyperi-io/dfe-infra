# ESO ClusterSecretStore — configures ESO to pull secrets from the target backend.
# For local/Rancher: OpenBao (Vault-compatible) AppRole auth.
# For cloud targets: replace provider block with aws/gcp/azure provider (per cloud.yaml values).
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: dfe-secret-store
spec:
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
