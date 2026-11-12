# The edge module

The edge module decides whether traffic from outside reaches a DFE workload. It's the public gateway (`helm/edge/gateway`) plus the fleet tunnel (culvert's published thin chart, beside `helm/charts/dfe-extras`), deployed as one add-on by `argocd/appsets/layer2-edge.yaml`.

It's on by default. Without it a deployment reaches nothing from outside. `edge.enabled: false` generates no Application, and the apps stay on ClusterIP behind whatever you bring. Turn it off AFTER a load balancer exists and you ORPHAN that load balancer, so the way off is destroy first, then disable.

Turning it off also takes external-dns's and the load balancer controller's IAM identities. Both keep running and report Ready, but they fail AccessDenied and the PRIVATE zone stops being published. Only the tunnel's PKI volume survives (`Prune=false,Delete=false`).

The tier table is per flavour, in `argocd/values/edge-<flavour>.yaml`.

## The five groups

Every surface falls into exactly one group:

- (a) dfe-ui and dfe-hyperdx. The product's own console and the search it frames. Public on a cloud deploy to the addresses `exposure.groups.console.allow` names, and to none while that list is empty.
- (b) The engine API. It sits on dfe-ui's own hostname, because the browser calls it at that origin. Its browser families follow the console group. Its CLI families, `/openapi.json` and SCIM are a second route in the api group, with its own list.
- (c) The admin UIs: Argo CD, Kafbat, Forgejo, links, Cruise Control. Off one at a time, behind a class-wide kill switch.
- (d) The ingest doors: the receiver and the OTLP route, plus the tunnel that reaches the receiver without a public address.
- (e) Everything else: Kafka, ClickHouse, Keeper, PostgreSQL, OpenBao, the Kubernetes API. Never exposed, on any flavour.

## The three tiers

1. On by default, where a DDoS attempt can't blow the budget.
2. Opt-in. Most carry a spend line. The engine's two family switches and OTLP ingress are opt-in for what they EXPOSE, and cost nothing.
3. Not offered. No key here reaches one.

Buckets are [aws.md](aws.md#how-costs-are-described)'s: relative to the cluster's own compute, the pricing model named, never a rate.

## AWS

| Mechanism | Group | Tier | Default | Dial key | Cost |
|---|---|---|---|---|---|
| Public gateway listener on an NLB | a b c | 1 | on | `edge.product.public` | XS hourly |
| Engine API on the product hostname, browser families | b | 1 | on | `edge.engine_api.with_product` | none |
| The CLI's families, and `/openapi.json` | b | 2 | off | `edge.engine_api.cli_families_public` | no spend |
| SCIM, `/api/v1/scim/v2` | b | 2 | off | `edge.engine_api.scim_public` | no spend |
| TLS floor and HSTS | a b | 1 | on | `edge.product.tls` | none |
| Internal-name certificates from the internal CA, because public ACME cannot validate a private zone | a b c | 1 | `dfe-internal-ca` | `tls.issuerName` (`tls.acme.*` or `tls.vault.*` for a browser-trusted leaf) | none |
| Rate limit, per proxy replica | a b | 1 | on | `edge.product.rate_limit` | none |
| CIDR filter per group, at Envoy (web) and the load balancer (the union, everything) | a b c d | 1 | empty, so every web route answers 403 | `exposure.groups.<group>.allow` and `.deny` in the deploy repo (`edge.product.allowed_cidrs` is the dial's note of it) | none |
| A CDN's `X-Forwarded-For` believed on the public listeners | a b | 2 | none believed | `edge.product.trusted_proxy_cidrs` | no spend |
| Edge OIDC on the admin routes | c | 1 | on | `edge.admin_uis.oidc` | none |
| Admin UI routes, one at a time | c | 1 | off | `edge.admin_uis.public.*` | none |
| Class-wide admin kill switch | c | 1 | off | `edge.admin_uis.external` | none |
| Receiver on its own load balancer, once senders are named | d | 2 | `loadbalancer`, closed while `allow` is empty | `edge.groups.ingest` (`exposure.groups.ingest`) | L, per GB processed |
| Elastic IPs for the receiver's NLB | d | 1 | on | `edge.aws.receiver_eips` | XS hourly, while closed too |
| Receiver on ClusterIP, tunnel-reached | d | 1 | off | `edge.groups.ingest.door: tunnel` | none |
| Tunnel on a NodePort | d | 1 once on | off | the engine's instance values file | none |
| An Elastic IP forwarder in front of it | d | 2 | `byo` | `edge.ingest.tunnel.address.mode` (`forwarder` is not tested in this release, [edge-vpn.md](edge-vpn.md#the-tunnels-address-on-aws)) | XS hourly |
| Admin reach-back to one appliance, not tested in this release | d | 1 | on | `edge.ingest.tunnel.admin_peer` | XS hourly |
| OTLP ingress on `otel.<domain>`, bearer token only | d | 2 | off | `edge.ingest.otel.enabled` | no spend |
| A CDN or managed WAF in front | a | 2 | `none` | `edge.product.waf.mode` | S, per GB and per request |
| Shield Advanced, Global Accelerator | a | 3 | absent | -- | -- |
| Group (e) | e | 3 | absent | -- | -- |

Public web access is default-deny on every cloud flavour ([edge-auth-cloud.md](../edge-auth-cloud.md#internet-facing-defaults)). The load-balancer half fences the WHOLE front door, so it takes the ingest group's list into its union while an ingest route renders. The aws root applies both dial keys. It adds every NAT gateway's address to the list, so the cluster's own callers on the public hostname still get in, and it passes `edge.product.trusted_proxy_cidrs` through as it stands. It trusts no subnet of its own. The NLB hands Envoy the client as the TCP peer, so no host in the VPC can name a client with a forged `X-Forwarded-For` ([edge-auth-cloud.md](../edge-auth-cloud.md#the-client-address-the-fence-judges)). A guarded test run replaces the list with its runner's /32.

### What the engine API's split gets wrong

The families are chart data (`routes.dfeEngine`), not a dial list, because they move with every router the engine adds. The five cases:

- A `PathPrefix` matches whole path elements, so `/api/v1/auth` never matches `/api/v1/authoring` and `/api/v1/sample` never matches `/api/v1/samples`.
- `GET /api/v1/auth/oidc/{provider}/callback` is a top-level browser navigation, so an edge login in front of it swallows the engine's own external-IdP login. The two are ALTERNATIVES on a hostname.
- The sampler is `POST /api/v1/sources/{source}/sample`, on the public `sources` prefix. Opening the `sample` family is a different thing.
- dfe-hyperdx reaches `/api/v1/hyperdx` from in-cluster, so its engine origin stays the internal hostname or the Service.
- `/api/v1/tasks` carries the only SSE stream, and stays private until the console wants it.

### Response hardening

HTTPS routes send a one-year HSTS header: public listeners on `edge.product.tls.hsts`, the wildcard on the gateway chart's `tls.edge.hsts`. A browser holding it gets no click-through on a certificate it doesn't trust, so a root re-minted on every rebuild would lock the admin out of the console. Leave `tls.edge.hsts` empty and it follows the edge issuer:

| Edge issuer | Wildcard HSTS |
|---|---|
| `tls.issuerName` is `tls.internalCA.issuerName`, `tls.internalCA.persist` off | off |
| the same, `tls.internalCA.persist` on | on |
| Vault/OpenBao PKI, ACME, or any other issuer | on |

An explicit `true` or `false` wins over the table. `edge.product.tls.hsts` doesn't follow it.

`bootstrap.sh` sets `tls.internalCA.persist.enabled=true` only for its own pre-apply of the root restore, and no appset sets it for Argo's gateway app. If you persist the root (`DFE_CA_PERSIST=true`), set `tls.internalCA.persist.enabled: true` and the store name in your deploy repo's gateway overlay too. Without it Argo renders the persist objects and the wildcard's HSTS off.

`routes.<key>.hiddenPaths` answers a path with a 404 from the proxy. dfe-ui uses it to hide its unauthenticated `/metrics`. `/docs` and `/redoc` follow `api.docsEnabled` in `infra/common.yaml`, which the engine, the gateway and the links page all read.

## GCP and Azure

Every row above holds, with these differences. Neither has a tofu root yet, so both are values-only.

| Mechanism | Group | Tier | Default | Dial key | Cost |
|---|---|---|---|---|---|
| Tunnel on the chart's own LoadBalancer | d | 1 once on | off | `exposure.serviceType` | S, per GB |
| An address in front of the tunnel | d | 2 | none built | -- | -- |

If you bring the tunnel, you pay that cloud's per-GB rate until the flavour file carries the AWS NodePort override.

## On-prem

Every row holds except these. There's no cloud bill, so a tier-2 line here is about the node's NIC and disk.

| Mechanism | Group | Tier | Default | Dial key | Cost |
|---|---|---|---|---|---|
| Gateway and tunnel on MetalLB | a b c d | 1 | on | `envoyGateway.service`, `exposure.serviceType` | none |
| Receiver routed through the gateway | d | 1 | `gateway` | `exposure.groups.ingest.door` | none |
| Admin reach-back | d | 3 | absent | -- | -- |

The reach-back is cloud-only. On-prem the operator is already on the LAN.

## OTLP ingress

Telemetry from outside the cluster (edge agents, other estates) lands on `otel.<domain>`. It's OFF on every flavour. The stack's own senders reach the collector on its Service, on 4317/4318, and never need it.

Turn it on and the gateway routes `otel.<domain>` to a second collector receiver on 4319, which checks a bearer token before any pipeline sees the request. The token comes from your secret store through an ExternalSecret. There's no unauthenticated mode.

The dial's `edge.ingest.otel` block is the `otel.ingress:` block of the deploy repo's `infra/common.yaml`, key for key. Paste it there. The gateway and the collector are two Applications, and that's the one file both read:

```yaml
otel:
  ingress:
    enabled: true
    auth:
      remoteKey: <path of the token in the secret store>   # property: token
```

On with `remoteKey` empty is refused by `render_dial.py`, the gateway chart and the collector chart alike. Senders POST to `/v1/traces`, `/v1/metrics` or `/v1/logs` with `Authorization: Bearer <token>`. `dfe-ops otel-ingress` says whether the door is exposed and where, and the readiness gate and the access summary print the same lines.

## The tunnel's inbound, and reaching one appliance

Appliances DIAL IN and hold the tunnel open, so nothing on an appliance's own network is ever exposed. `exposure.loadBalancerSourceRanges` starts empty, which means every source address on earth. That's the normal case for a fleet. Per-client PKI, WireGuard peer keys and tls-crypt-v2 authenticate a client, and `peers.classes.appliance.isolation` stops one compromised appliance reaching the fleet through the hub.

culvert's client-to-client verdict drops an admin that dials in, so you reach one appliance with a route. `dfe-ops bastion hub <peer>` programs the tunnel's client range at the culvert pod and opens the logged Session Manager shell. `down` removes the range. The appliance must accept ssh on 22 and https on 443 on its tunnel interface. Run the isolation regression on every change to the class policy, because one appliance peer must still not reach another. `dfe-ops bastion join` is not available in this release, and `hub` is the way to reach an appliance. [edge-vpn.md](edge-vpn.md#reaching-an-appliance-from-the-bastion) carries the VPC routing and what bounds it.

## Proving it

`dfe-ops edge-probe` runs against the deployment from outside. The render cases need no cluster.

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
| HSTS on every HTTPS route, each listener on its own switch, the wildcard's default following the edge issuer, dfe-ui's `/metrics` a 404 on both faces | 1 | `scripts/tests/test_gateway_route_hardening.py` | render |
| `/docs` and `/redoc` routed and listed only where the engine serves them | 1 | `scripts/tests/test_engine_docs_surface.py` | render |
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

- [README.md](README.md) - the deploy layers and the values cascade
- [aws.md](aws.md) - the AWS deployment, and the cost vocabulary
- [edge-vpn.md](edge-vpn.md) - the tunnel itself
- [gateway-oidc.md](gateway-oidc.md) - edge OIDC, and its limits
- [toolbox.md](toolbox.md) - the bastion behind the reach-back
