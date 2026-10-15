# secrets -- module contract

REFERENCE. The store External Secrets Operator reads from, the credentials seeded into it,
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
| `kafka_username` | `string`, default null | Fills the `username` field of a `kafka/<provider>` seed that carries one, which no body generates either. Only `kafka/confluent-cloud` does: Confluent mints the API key that is the username. Null wherever the kafka seed carries no username. |
| `clickhouse_password` | `string`, default null | Sensitive. Fills the `password` field of the `clickhouse/external` seed, which no body generates: an external ClickHouse already holds its password. Null wherever the seeds carry no `clickhouse/external` entry. |
| `kms_key_arn` | `string` | The deployment's own key, from the cluster module. Bodies whose backend has no separate key ignore it. |
| `read_secret_arns` | `list(string)`, default `[]` | AWS-only (`aws-sm/`). Secrets outside the path that ESO also reads, by ARN, never writes: MSK's `AmazonMSK_<name>` SCRAM secret, the one copy of the Kafka credential on an msk deployment. |
| `recovery_window_days` | `number`, default 30 | AWS-only today (`aws-sm/`). The store's deletion delay. The root passes 0 for a `tags.lifecycle: ephemeral` deployment torn down and rebuilt under the same name, where the vendor's 30-day window would fail the second create on a name still pending deletion. |
| `cluster_name` | `string` | Which cluster's ESO and dfe-engine get their identities. |
| `engine_namespace` | `string` | AWS-only (`aws-sm/`). The namespace dfe-engine runs in, the naming module's `k8s_namespace`. Its `dfe-engine` service account gets the engine's identity. |
| `pod_identity_trust_policy_json` | `string` | From the cluster module, so this module mints a workload identity without knowing how the cloud expresses cluster trust. |

Secrets are created at `<prefix>/<project>/<env>/<seed name>`, and ESO's read
grant is fenced to exactly that path, less `engine/`. With no prefix the path
starts at `<project>/<env>`, which is the same shape the OpenBao body uses.

`<path>/engine/` belongs to dfe-engine: it creates, reads, updates and deletes the secrets it mints there (its JWT signing key, ClickHouse passwords and OIDC client secrets) through an identity of its own. No seed may be named under it, and ESO is denied it.

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

Confluent Cloud is the same shape one field wider. The vendor mints the API key
and secret inside the managed-kafka body, so the caller hands the key in as
`kafka_username` and the secret as `kafka_password`, and the
`kafka/confluent-cloud` seed's `username` field takes the first. A body refuses a
kafka seed that names a `username` with none supplied.

An external ClickHouse is the same rule for a server that already exists: the `clickhouse/external` seed's `password` field comes from `clickhouse_password`, and a body refuses that seed with no password supplied.

MSK takes no kafka seed at all. MSK requires its SCRAM secret to be named `AmazonMSK_<name>`, outside this path, so that secret is the one copy: the root hands its ARN in as `read_secret_arns`, and the kafka chart's app-namespace copy reads it through the unprefixed store bootstrap creates beside `dfe-secret-store`.

## Outputs

| Output | Type | Meaning |
|--------|------|---------|
| `store_config` | `object` | Everything a ClusterSecretStore needs and nothing a caller has to interpret: `provider`, `service`, `region`, `auth`, `prefix`. Each body fills the fields its provider block reads. `prefix` is the `prefix` input alone, never the full path: every chart's remoteRef key already starts with `<project>/<env>`. |
| `eso_role_arn` | `string` | The identity ESO assumes. Empty for a backend that authenticates some other way. |
| `engine_role_arn` | `string` | AWS-only (`aws-sm/`). The identity dfe-engine assumes: write under `engine_name_prefix`, and the deployment key used through Secrets Manager alone. |
| `engine_name_prefix` | `string` | AWS-only (`aws-sm/`). `<path>/engine`, which the root hands bootstrap as `DFE_ENGINE_SECRETS_PREFIX`. |

## Rules every body follows

- **The store is the source of truth, and ESO's grant is READ-ONLY except
  under `pki/`.** Two in-cluster writers exist, each fenced to its own prefix:
  the gateway chart's PushSecret persists the internal CA root under `pki/`,
  and dfe-engine writes what it mints under `engine/` with its own identity.
  Nothing else in-cluster writes the store. The engine's secrets sit outside
  tofu state, so a test run's are swept by their run tag and `dfe-ops teardown
  --with-terraform` force-deletes the rest before `tofu destroy`.
- **A generated value never leaves the module.** It goes into the store and
  into state; it is not an output.
- **Seeds are never rotated on re-apply.** Re-running the deploy must not log
  every user out or lock the fleet out of Kafka. Rotation is a deliberate,
  separate operation.
- **Read is fenced to the prefix.** A grant on the whole store is not a grant.
