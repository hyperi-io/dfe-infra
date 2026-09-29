# The edge module

The public gateway (`helm/edge/gateway`) and the fleet tunnel
(`helm/edge/culvert`) are one add-on, deployed by
`argocd/appsets/layer2-edge.yaml`: everything that decides whether traffic from
outside reaches a DFE workload.

It is ON by default: a deployment with no door reaches nothing from outside.
`edge.enabled: false` generates no Application, and the apps stay on ClusterIP
behind whatever the deployer brings. Turning it off AFTER a load balancer exists
ORPHANS it, so the off path is destroy, then disable.

It also takes external-dns's and the load balancer controller's IAM identities.
Both keep running, start failing AccessDenied while reporting Ready, and the
PRIVATE zone stops being published. Only the tunnel's PKI volume survives
(`Prune=false,Delete=false`).

The tier table is per flavour, in `argocd/values/edge-<flavour>.yaml`.

## The five groups

Every surface falls into exactly one:

- **(a) dfe-ui** -- the product's own console, public on a cloud deploy.
- **(b) the engine API** -- on dfe-ui's own hostname, because the browser calls
  it at that origin. The public route carries the path families a browser uses;
  the CLI's families and SCIM are each their own opt-in.
- **(c) the admin UIs** -- Argo CD, Kafbat, HyperDX, Forgejo, links, Cruise
  Control. Off one at a time, behind a class-wide kill switch.
- **(d) the ingest doors** -- the receiver and the OTLP route, plus the tunnel
  that reaches the receiver without a public address.
- **(e) everything else** -- Kafka, ClickHouse, Keeper, PostgreSQL, OpenBao, the
  Kubernetes API. Never exposed, on any flavour.

## The three tiers

1. **On by default**, where a DDoS attempt cannot blow the budget.
2. **Opt-in.** Most carry a spend line. The engine's two family switches and
   OTLP ingress are opt-in for what they EXPOSE, and cost nothing.
3. **Not offered.** No key here reaches one.

Buckets are [aws.md](aws.md#how-costs-are-described)'s: relative to the cluster's
own compute, the pricing model named, never a rate.

## AWS

| Mechanism | Group | Tier | Default | Dial key | Cost |
|---|---|---|---|---|---|
| Public gateway listener on an NLB | a b c | 1 | on | `edge.product.public` | XS hourly |
| Engine API on the product hostname, browser families | b | 1 | on | `edge.engine_api.with_product` | none |
| The CLI's families, and `/openapi.json` | b | 2 | off | `edge.engine_api.cli_families_public` | no spend |
| SCIM, `/api/v1/scim/v2` | b | 2 | off | `edge.engine_api.scim_public` | no spend |
| TLS floor and HSTS | a b | 1 | on | `edge.product.tls` | none |
| Rate limit, per proxy replica | a b | 1 | on | `edge.product.rate_limit` | none |
| CIDR filter, at Envoy and the load balancer | a b c d | 1 | unset | `edge.product.allowed_cidrs` | none |
| Edge OIDC on the admin routes | c | 1 | on | `edge.admin_uis.oidc` | none |
| Admin UI routes, one at a time | c | 1 | off | `edge.admin_uis.public.*` | none |
| Class-wide admin kill switch | c | 1 | off | `edge.admin_uis.external` | none |
| Receiver on ClusterIP, tunnel-reached | d | 1 | `vpn` | `edge.ingest.receiver.mode` | none |
| Receiver on its own load balancer | d | 2 | off | `edge.ingest.receiver.public` | L, per GB processed |
| Tunnel on a NodePort | d | 1 once on | off | the engine's instance values file | none |
| An Elastic IP forwarder in front of it | d | 2 | `byo` | `edge.ingest.tunnel.address.mode` (`forwarder` is unproven, [edge-vpn.md](edge-vpn.md#the-tunnels-address-on-aws)) | XS hourly |
| Admin reach-back to one appliance | d | 1 | on | `edge.ingest.tunnel.admin_peer` | XS hourly |
| OTLP ingress on `otel.<domain>`, bearer token only | d | 2 | off | `edge.ingest.otel.enabled` | no spend |
| A CDN or managed WAF in front | a | 2 | `none` | `edge.product.waf.mode` | S, per GB and per request |
| Shield Advanced, Global Accelerator | a | 3 | absent | -- | -- |
| Group (e) | e | 3 | absent | -- | -- |

The CIDR filter fences the WHOLE front door: one gateway Service carries every
listener, so an allow-list narrow enough to lock an admin UI down blocks agent
ingest too. Set `edge.product.trusted_proxy_cidrs` with it, or the client address
comes from a header the caller writes.

### What the engine API's split gets wrong

The families are chart data (`routes.dfeEngine`), not a dial list: they move with
every router the engine adds. Five traps:

- A `PathPrefix` matches whole path elements, so `/api/v1/auth` never matches
  `/api/v1/authoring` and `/api/v1/sample` never matches `/api/v1/samples`.
- `GET /api/v1/auth/oidc/{provider}/callback` is a top-level browser navigation,
  so an edge login in front of it swallows the engine's own external-IdP login.
  The two are ALTERNATIVES on a hostname.
- The sampler is `POST /api/v1/sources/{source}/sample`, on the public `sources`
  prefix -- opening the `sample` family is a different thing.
- The HyperDX fork reaches `/api/v1/hyperdx` from in-cluster, so its engine
  origin stays the internal hostname or the Service.
- `/api/v1/tasks` carries the only SSE stream, and stays private until the
  console wants it.

## GCP and Azure

Every row above holds, with these differences. Neither has a tofu root yet, so
both are values-only.

| Mechanism | Group | Tier | Default | Dial key | Cost |
|---|---|---|---|---|---|
| Tunnel on the chart's own LoadBalancer | d | 1 once on | off | `exposure.serviceType` | S, per GB |
| An address in front of the tunnel | d | 2 | none built | -- | -- |

A deployment that brings the tunnel pays that cloud's per-GB rate until the
flavour file carries the AWS NodePort override.

## On-prem

Every row holds except these. There is no cloud bill, so a tier-2 line here is
about the node's NIC and disk.

| Mechanism | Group | Tier | Default | Dial key | Cost |
|---|---|---|---|---|---|
| Gateway and tunnel on MetalLB | a b c d | 1 | on | `envoyGateway.service`, `exposure.serviceType` | none |
| Receiver routed through the gateway | d | 1 | `internal` | `routes.receiver.enabled` | none |
| Edge certificates from the internal CA | a b c | 1 | `dfe-internal-ca` | `tls.issuerName` | none |
| Admin reach-back | d | 3 | absent | -- | -- |

The reach-back is cloud-only: on-prem the operator is already on the LAN.

## OTLP ingress

Telemetry from outside the cluster -- edge agents, other estates -- lands on
`otel.<domain>`. It is OFF on every flavour. The stack's own senders reach the
collector on its Service, on 4317/4318, and never need it.

On, the gateway routes `otel.<domain>` to a second collector receiver on 4319
that checks a bearer token before any pipeline sees the request. The token comes
from the deployment's secret store through an ExternalSecret. There is no
unauthenticated mode.

The dial's `edge.ingest.otel` block is the `otel.ingress:` block of the deploy
repo's `infra/common.yaml`, key for key. Paste it there, because the gateway and
the collector are two Applications and that is the one file both read:

```yaml
otel:
  ingress:
    enabled: true
    auth:
      remoteKey: <path of the token in the secret store>   # property: token
```

On with `remoteKey` empty is refused by `render_dial.py`, the gateway chart and
the collector chart alike. Senders POST to `/v1/traces`, `/v1/metrics` or
`/v1/logs` with `Authorization: Bearer <token>`. `dfe-ops otel-ingress` says
whether the door is exposed and where, and the readiness gate and the access
summary print the same lines.

## The tunnel's inbound, and reaching one appliance

Appliances DIAL IN and hold the tunnel open, so nothing on an appliance's own
network is ever exposed. `exposure.loadBalancerSourceRanges` starts empty --
every source address on earth, which is the normal case for a fleet. Per-client
PKI, WireGuard peer keys and tls-crypt-v2 authenticate a client;
`peers.classes.appliance.isolation` stops one compromised appliance reaching the
fleet through the hub.

**Reaching one appliance is a ROUTE, not a dial-in.** culvert's client-to-client
verdict drops an admin that dials in, so `dfe-ops bastion hub <peer>` programs
the tunnel's client range at the culvert pod and opens the logged Session Manager
shell; `down` removes the range. The appliance accepts ssh on 22 and https on 443
on its tunnel interface. Run the isolation regression on every change to the
class policy: one appliance peer still cannot reach another. None of it has been
exercised against a cluster.
[edge-vpn.md](edge-vpn.md#reaching-an-appliance-from-the-bastion) carries the VPC
routing, what bounds it, and the `hyperi-io/culvert#40` peer shape that replaces
it.

## Proving it

`dfe-ops edge-probe` proves from outside what can be proven from outside; the
render cases need no cluster.

| Claim | Tier | Proof | Venue |
|---|---|---|---|
| The module off renders no door | 1 | render case | render |
| Admin UIs off by default, kill switch beats a route flag | 1 | `scripts/test-route-exposure.sh` | render |
| Every admin UI the gateway lists loads: no redirect loop, no 5xx, no 4xx but 401/403/404 | 1 | `dfe-ops admin-probe`, run by the readiness gate | kind, on-prem |
| A public route with no auth is refused | 1 | `scripts/test-route-exposure.sh` | render |
| The engine's public route: browser families, the opt-ins add theirs, never `/docs` | 1 | `scripts/test-route-exposure.sh` | render |
| A browser family answers and a private one 404s | 1 | `dfe-ops edge-probe` | kind, on-prem |
| Tier-3 keys are absent | 1 | render case | render |
| TLS floor, HSTS, rate limit, CIDR filter | 1 | `dfe-ops edge-probe` | kind, on-prem |
| The receiver is private in `vpn` mode | 1 | `dfe-ops edge-probe` | kind, on-prem |
| OTLP ingress renders no route while off, and refuses to render on with no token | 1 | `scripts/tests/test_otel_ingress.py`, `scripts/test-route-exposure.sh` | render |
| `otel.<domain>` answers 404 while off, and 401 to a tokenless request while on | 1 | `dfe-ops edge-probe` | kind, on-prem, AWS |
| Edge OIDC login and group check | 1 | `dfe-ops idp` and the onboarding suite | on-prem |
| A client reaches the receiver only through the tunnel | 1 | dial in, post, then post direct and fail | on-prem |
| One appliance peer cannot reach another | 1 | the isolation regression | on-prem |
| The CIDR filter bites at the load balancer | 1 | `dfe-ops edge-probe` off-list | AWS |
| `preserve_client_ip` and the frontend security group | 1 | describe the load balancer | AWS |
| The public certificate is issued by DNS-01 | 1 | chain assert on the public name | AWS |
| The reach-back reaches an appliance | 1 | `dfe-ops bastion hub`, then ssh | AWS |

## Related

- [index.md](index.md) - the deploy layers and the values cascade
- [aws.md](aws.md) - the AWS deployment, and the cost vocabulary
- [edge-vpn.md](edge-vpn.md) - the tunnel itself
- [gateway-oidc.md](gateway-oidc.md) - edge OIDC, and its limits
- [toolbox.md](toolbox.md) - the bastion behind the reach-back
