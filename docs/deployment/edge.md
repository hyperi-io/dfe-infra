# The edge module

Everything that decides whether traffic from outside the cluster reaches a DFE
workload is one add-on: the public gateway (`helm/edge/gateway`) and the fleet
tunnel (`helm/edge/culvert`), deployed together by
`argocd/appsets/layer2-edge.yaml`.

It is ON by default, because a deployment with no door reaches nothing from
outside. `edge.enabled: false` is the whole-module off switch and generates no
Application at all; the apps then stay on ClusterIP behind whatever the deployer
brings. Turning it off AFTER a load balancer exists ORPHANS that load balancer,
so the off path is destroy, then disable.

Switching it off also takes external-dns's and the load balancer controller's
IAM identities, which live in this module. Both controllers keep running -- they
are layer-1 Applications gated on other facts -- and start failing AccessDenied
while reporting Ready, so the PRIVATE zone stops being published too. The
tunnel's PKI volume survives (`Prune=false,Delete=false`); nothing else does.

The tier table is per flavour. The same dial keys carry different defaults on
`aws`, `gcp`, `azure` and `onprem`, and the per-flavour file is
`argocd/values/edge-<flavour>.yaml`.

## The five groups

Every surface DFE runs falls into exactly one:

- **(a) dfe-ui** -- the product's own console, public on a cloud deploy.
- **(b) the engine API** -- exposed WITH dfe-ui on one hostname today. Splitting
  it by path family waits on the engine team naming the families.
- **(c) the admin UIs** -- Argo CD, Kafbat, HyperDX, Forgejo, the links page,
  Cruise Control. Off, one at a time, behind a class-wide kill switch.
- **(d) the ingest doors** -- the receiver and the OTLP route, plus the tunnel
  that reaches the receiver without a public address.
- **(e) everything else** -- Kafka, ClickHouse, Keeper, PostgreSQL, OpenBao and
  the Kubernetes API. Never exposed, on any flavour.

## The three tiers

1. **On by default**, where a DDoS attempt cannot blow the budget, and never on
   a volume door.
2. **Opt-in, with a spend line** in buckets.
3. **Not offered.** No key in this module reaches one.

Cost buckets are the ones
[aws.md](aws.md#how-costs-are-described) defines -- relative to the cluster's
own compute, with the pricing model named and never a rate.

## AWS

| Mechanism | Group | Tier | Default | Dial key | Cost |
|---|---|---|---|---|---|
| Public gateway listener on an NLB | a b c | 1 | on | `edge.product.public` | XS hourly |
| TLS floor and HSTS | a b | 1 | on | `edge.product.tls` | none |
| Rate limit, per proxy replica | a b | 1 | on | `edge.product.rate_limit` | none |
| CIDR filter, at Envoy and the load balancer | a b c d | 1 | unset | `edge.product.allowed_cidrs` | none |
| Edge OIDC on the admin routes | c | 1 | on | `edge.admin_uis.oidc` | none |
| Admin UI routes, one at a time | c | 1 | off | `edge.admin_uis.public.*` | none |
| Class-wide admin kill switch | c | 1 | off | `edge.admin_uis.external` | none |
| Receiver on ClusterIP, tunnel-reached | d | 1 | `vpn` | `edge.ingest.receiver.mode` | none |
| Receiver on its own load balancer | d | 2 | off | `edge.ingest.receiver.public` | L, per GB processed |
| Tunnel on a NodePort | d | 1 once on | off | the engine's instance values file | none |
| An Elastic IP forwarder in front of it | d | 2 | `byo` | `edge.ingest.tunnel.address.mode` (`forwarder` not usable end to end yet, see [edge-vpn.md](edge-vpn.md#the-tunnels-address-on-aws)) | XS hourly |
| Admin reach-back to one appliance | d | 1 | on | `edge.ingest.tunnel.admin_peer` | XS hourly |
| OTLP route, private | d | 1 | private | `edge.ingest.otel.public` | none |
| A CDN or managed WAF in front | a | 2 | `none` | `edge.product.waf.mode` | S, per GB and per request |
| Shield Advanced, Global Accelerator | a | 3 | absent | -- | -- |
| Group (e) | e | 3 | absent | -- | -- |

The CIDR filter fences the WHOLE front door: one gateway Service carries every
listener, so an allow-list narrow enough to lock an admin UI down also blocks
agent ingest. Set `edge.product.trusted_proxy_cidrs` alongside it, or the client
address comes from a header the caller writes.

## GCP and Azure

Every row above holds, with these differences. Neither flavour has a tofu root
yet, so both are values-only until one exists.

| Mechanism | Group | Tier | Default | Dial key | Cost |
|---|---|---|---|---|---|
| Tunnel on the chart's own LoadBalancer | d | 1 once on | off | `exposure.serviceType` | S, per GB |
| An address in front of the tunnel | d | 2 | none built | -- | -- |

The tunnel keeps a real LoadBalancer on both, so a deployment that brings the
tunnel pays that cloud's per-GB rate on the tunnel's bytes until the flavour
file carries the AWS NodePort override.

## On-prem

Every row holds except these. There is no cloud bill at all, so a tier-2 line
here is about the node's NIC and disk.

| Mechanism | Group | Tier | Default | Dial key | Cost |
|---|---|---|---|---|---|
| Gateway and tunnel on MetalLB | a b c d | 1 | on | `envoyGateway.service`, `exposure.serviceType` | none |
| Receiver routed through the gateway | d | 1 | `internal` | `routes.receiver.enabled` | none |
| Edge certificates from the internal CA | a b c | 1 | `dfe-internal-ca` | `tls.issuerName` | none |
| Admin reach-back | d | 3 | absent | -- | -- |

The reach-back is a cloud mechanism only: on-prem the operator is already on the
LAN and the module supplies nothing.

## The tunnel's inbound, and reaching one appliance

Appliances DIAL IN and hold the tunnel open, so nothing on an appliance's own
network is ever exposed. `exposure.loadBalancerSourceRanges` is the allow-list
on that door and starts empty, which is every source address on earth -- a fleet
dialling in from anywhere is the normal case. Per-client PKI, WireGuard peer
keys and tls-crypt-v2 are what actually authenticate a client;
`peers.classes.appliance.isolation` is what stops one compromised appliance
reaching the rest of the fleet through the hub.

**Reaching one appliance, today.** culvert's admin exception matches a source
arriving off the pod's ethernet side, and its client-to-client verdict runs
first, so an admin that DIALS IN is dropped before any rule naming it is
reached. `dfe-ops bastion hub <peer>` therefore routes rather than dials: it
programs the tunnel's client range at the culvert pod's own address, refreshes
that route on every call because a roll moves the pod, and opens the LOGGED
Session Manager shell. The subnet the toolbox lands in is what
`peers.classes.admin.adminCIDRs` names, and the chart refuses a range inside the
client range or the whole internet. A VPC also has to route the client range at
the culvert node's interface, with that interface's source/destination check
off, or the packet never leaves the subnet.

**That subnet is wider than the bastion**, and none of the reach-back has been
exercised against a cluster.
[edge-vpn.md](edge-vpn.md#reaching-an-appliance-from-the-bastion) carries how
wide, what bounds it, and the two claims a live run still has to prove.

**Once `hyperi-io/culvert#40` lands**, the admin becomes a peer with a one-way
isolation exception, minted per session and never stored, and `dfe-ops bastion
join` stops refusing. Run the isolation regression on every change to the class
policy: one appliance peer still cannot reach another.

**The appliance side.** An appliance has to accept ssh on 22 and https on 443 on
its tunnel interface, from the admin's address.

## Proving it

`dfe-ops edge-probe` runs from the operator's machine and proves from outside
what can be proven from outside. The render cases need no cluster at all.

| Claim | Tier | Proof | Venue |
|---|---|---|---|
| The module off renders no door | 1 | render case | render |
| Admin UIs off by default, kill switch beats a route flag | 1 | `scripts/test-route-exposure.sh` | render |
| A public route with no auth is refused | 1 | `scripts/test-route-exposure.sh` | render |
| Tier-3 keys are absent | 1 | render case | render |
| TLS floor, HSTS, rate limit, CIDR filter | 1 | `dfe-ops edge-probe` | kind, Cluster B |
| The receiver is private in `vpn` mode | 1 | `dfe-ops edge-probe` | kind, Cluster B |
| The OTLP route is private on cloud | 1 | `dfe-ops edge-probe` | kind |
| Edge OIDC login and group check | 1 | `dfe-ops idp` plus the onboarding suite | Cluster B |
| A client reaches the receiver through the tunnel and nothing else | 1 | dial in, post, then post direct and fail | Cluster B |
| One appliance peer cannot reach another | 1 | the isolation regression | Cluster B |
| The CIDR filter bites at the load balancer | 1 | `dfe-ops edge-probe` off-list | AWS |
| `preserve_client_ip` and the frontend security group | 1 | describe the load balancer | AWS |
| The public certificate is issued by DNS-01 | 1 | chain assert on the public name | AWS |
| The reach-back reaches an appliance | 1 | `dfe-ops bastion hub`, then ssh | AWS |

## Related

- [index.md](index.md) - the deploy layers and the values cascade
- [aws.md](aws.md) - the AWS deployment, and the cost vocabulary
- [edge-vpn.md](edge-vpn.md) - the tunnel itself
- [gateway-oidc.md](gateway-oidc.md) - edge OIDC, and what Envoy Gateway cannot do
- [toolbox.md](toolbox.md) - the bastion the reach-back runs from
