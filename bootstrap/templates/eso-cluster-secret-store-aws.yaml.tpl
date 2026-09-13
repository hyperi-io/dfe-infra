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
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: dfe-secret-store
spec:
  provider:
    aws:
      service: SecretsManager
      region: "${DFE_SECRETS_REGION}"
      prefix: "${DFE_SECRETS_PREFIX_PATH}"
