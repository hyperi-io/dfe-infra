# License Review

**Policy:** https://github.com/hyperi-io/licensing
**Status:** Initial review pending

## Review Checklist

Before each release, audit all dependency licenses against the approved policy.
Flag GPL/AGPL/SSPL — these require legal sign-off before inclusion.

## Terraform Providers

| Provider | License | Status |
|----------|---------|--------|
| hashicorp/null | MPL-2.0 | Approved |
| (others added per module) | | |

## Helm Chart Dependencies (upstream)

| Chart | License | Status |
|-------|---------|--------|
| cert-manager | Apache-2.0 | Approved |
| external-secrets | Apache-2.0 | Approved |
| envoy-gateway | Apache-2.0 | Approved |
| keda | Apache-2.0 | Approved |
| argo-cd | Apache-2.0 | Approved |
| strimzi-kafka-operator | Apache-2.0 | Approved |
| cnpg | Apache-2.0 | Approved |
| clickhouse-operator | Apache-2.0 | Approved |
| stakater-reloader | Apache-2.0 | Approved |
| metrics-server | Apache-2.0 | Approved |
| hyperdx | MIT | Approved |
| ferretdb | Apache-2.0 | Approved |
| valkey (bitnami) | Apache-2.0 | Approved |
| otel-collector | Apache-2.0 | Approved |
| vpa (vertical-pod-autoscaler) | Apache-2.0 | Approved |
| kedify-agent | TBD — review before production | Pending |

## Container Images

To be audited per release. Flag any images with commercial-only licenses.

## Notes

- Kedify OTEL Scaler license must be verified before production use (see Open Question 1 in spec).
- All Apache-2.0 and MIT licenses are pre-approved per hyperi-io/licensing policy.
