# secrets -- module contract

The store External Secrets Operator reads from, the credentials seeded into it,
and the identity ESO uses to reach it. One body per backend:

| Body | Status | Store | ESO auth |
|------|--------|-------|----------|
| `aws-sm/` | built | AWS Secrets Manager | EKS Pod Identity |
| `openbao/` | not built | OpenBao KV v2 | AppRole |
| `gcp-sm/` | not built | GCP Secret Manager | Workload Identity Federation |
| `azure-kv/` | not built | Azure Key Vault | Entra Workload ID |

The existing `tf-secrets` module is the openbao body in all but name, and its
outputs are why this contract exists: it exports `vault_addr`, `eso_role_id`,
`eso_secret_id`, `eso_policy_name` and `kv_mount_path` -- an OpenBao-shaped
contract no cloud body can satisfy. The fix is a store-shaped one:
`store_config` is OPAQUE to the caller and carries whatever the backend's
ClusterSecretStore provider block needs.

## Inputs

| Input | Type | Meaning |
|-------|------|---------|
| `project` | `string` | Path segment, e.g. `dfe`. |
| `env` | `string` | Path segment, e.g. `test`. |
| `prefix` | `string` | The deployment dial's `secrets.ref`. The first path segment, and the boundary ESO's read grant is fenced to. OPTIONAL -- empty is a store that holds this deployment alone, and leaves the path at `<project>/<env>`. |
| `seeds` | `map(map(string))` | Secret name -> field -> value. An EMPTY value means "generate one here and never show it to the caller", which is how a password lands in the store without passing through a tfvars file or a plan output. |
| `kafka_password` | `string` | Sensitive. Fills the `password` field of the `kafka/<provider>` seed, which no body generates -- see below. Null only where the seeds carry no kafka entry at all. |
| `kms_key_arn` | `string` | The deployment's own key, from the cluster module. Bodies whose backend has no separate key ignore it. |
| `cluster_name` | `string` | Which cluster's ESO gets the identity. |
| `pod_identity_trust_policy_json` | `string` | From the cluster module, so this module mints a workload identity without knowing how the cloud expresses cluster trust. |

Secrets are created at `<prefix>/<project>/<env>/<seed name>`, and ESO's read
grant is fenced to exactly that path. With no prefix the path starts at
`<project>/<env>`, which is the same shape the OpenBao body uses.

## The one credential no body generates

A MANAGED broker is CREATED with its SCRAM password, so that value has to exist
before either the broker or the store is written -- and a value generated inside
this module never leaves it, by the rule below. So the Kafka password is the
CALLER's on every backend and every broker: it generates it once and hands the
same value to the store here and to the broker module beside it.

It is not an override the module decides about, because it cannot: which fields
a body generates is a `for_each` and has to be known at plan, while the password
is not known until apply. The seed NAMES carry the decision instead -- a
`kafka/<provider>` seed's `password` field comes from `kafka_password`, and a
body refuses seeds that name one with no password supplied.

## Outputs

| Output | Type | Meaning |
|--------|------|---------|
| `store_config` | `object` | Everything a ClusterSecretStore needs and nothing a caller has to interpret: `provider`, `service`, `region`, `auth`, `prefix`. Each body fills the fields its provider block reads. |
| `eso_role_arn` | `string` | The identity ESO assumes. Empty for a backend that authenticates some other way. |

## Rules every body follows

- **The store is the source of truth, and ESO's grant is READ-ONLY.** A
  PushSecret from inside the cluster is refused by design, not by accident --
  nothing in-cluster mints a credential.
- **A generated value never leaves the module.** It goes into the store and
  into state; it is not an output.
- **Seeds are never rotated on re-apply.** Re-running the deploy must not log
  every user out or lock the fleet out of Kafka. Rotation is a deliberate,
  separate operation.
- **Read is fenced to the prefix.** A grant on the whole store is not a grant.
