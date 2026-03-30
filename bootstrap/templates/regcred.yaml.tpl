# Rendered by bootstrap.sh to create imagePullSecret for JFrog registry.
# DFE_REGISTRY_AUTH must be pre-computed as base64(user:token) by bootstrap.sh.
apiVersion: v1
kind: Secret
metadata:
  name: dfe-regcred
  namespace: ${TARGET_NAMESPACE}
type: kubernetes.io/dockerconfigjson
stringData:
  .dockerconfigjson: |
    {
      "auths": {
        "${DFE_REGISTRY_HOST}": {
          "username": "${DFE_REGISTRY_USER}",
          "password": "${DFE_REGISTRY_TOKEN}",
          "auth": "${DFE_REGISTRY_AUTH}"
        }
      }
    }
