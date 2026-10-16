# registry-cache -- module contract

REFERENCE. A pull-through cache inside the deployment's own cloud account that every node pulls container images through, so upstream rate limits stop failing a deploy. The cache fetches each image once and serves every later pull itself. The same pattern ran for earlier DFE 2.1 customer deployments. One body per cloud:

| Body | Status | Cache |
|------|--------|-------|
| `aws/` | built | ECR pull-through cache rules |
| `gcp/` | not built | Artifact Registry remote repositories, one per upstream, credentials from Secret Manager |
| `azure/` | not built | ACR artifact cache rules, credential sets in Key Vault |

Every body takes the inputs below and produces the outputs below. The caller is the per-cloud root, which hands `node_user_data` to the kubernetes-cluster module and to the Karpenter node class.

The aws root's dial (`registry_cache`, off by default) sends the upstreams ECR caches with no credential: quay.io, registry.k8s.io and public.ecr.aws. It sends docker.io or ghcr.io only where the dial names a Secrets Manager credential for it, an opt-in off the default deploy path (`docs/deployment/aws-operations.md`). Without one their images pull straight from upstream, anonymously.

## How nodes use it

Image references are never rewritten. Each node gets one containerd mirror per cached registry host (`/etc/containerd/certs.d/<host>/hosts.toml`) with the upstream as `server`, so containerd tries the cache first and the upstream second. A node that cannot reach the cache pulls exactly as it would with none.

On AWS the mirror needs an ECR login, and kubelet's ECR credential provider matches image names, so it never sends one for `quay.io/...`. Widening its `matchImages` would hand the same ECR token to the upstream fallback too, because kubelet leaves the credential's server address empty. So the mirror entry carries its own `Authorization` header, refreshed from `aws ecr get-login-password` by a node timer at half the token's 12-hour life.

Rewriting image repositories in chart values was rejected: every third-party chart spells its image differently, operators start images of their own, and an outage of the cache would then fail every pull.

## Inputs

| Input | Type | Meaning |
|-------|------|---------|
| `provision` | `object({ account, region })` | Where the cache lives, the deployment's own account and region. |
| `name` | `string` | Name prefix for the IAM role the body creates. |
| `namespace` | `string` | Repository path every rule prefix starts with. Two deployments in one account and region set different ones. |
| `upstreams` | `map(object({ upstream_url, host, requires_credential, credential_arn }))` | Keyed by the rule's prefix under `namespace`. An upstream whose `requires_credential` is true and whose `credential_arn` is empty gets no rule and no mirror. |
| `pull_expiry_days` | `number` | Days a cached image is kept after its last pull. |
| `kms_key_arn` | `string` | The deployment's key; every cached repository is encrypted with it. |
| `node_role_arns` | `map(string)` | The node roles that pull through the cache, keyed by a name known at plan. |
| `tags` | `map(string)` | Tags every cached repository is created with. |
| `permissions_boundary`, `iam_path` | `string`, default null | Applied to the role the body creates. |

## Outputs

| Output | Type | Meaning |
|--------|------|---------|
| `node_user_data` | `string` | The shell script a node runs before kubelet starts. Empty when nothing is cached. |
| `registry` | `string` | The registry host the cache is served from. |
| `mirrors` | `map(string)` | Cached registry host to the path containerd reads it from. |
| `repository_tags` | `map(map(string))` | Cached upstream key to the tags every repository the cloud creates for it carries: `tags`, run tags included. |
| `uncached` | `list(string)` | Upstream keys skipped for want of a credential. |

## Rules every body follows

- **Credentials are inputs, never minted here.** An upstream that needs one is skipped until the deployer names it.
- **The cache only removes pulls.** No failure of the cache may stop a node pulling from its upstream.
- **Destroy leaves nothing.** Repositories the cloud creates on a first pull are deleted with the rules that made them.
