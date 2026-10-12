# Edge exposure and authentication -- cloud specifics

The model, the identity plane and on-prem behaviour:
[EDGE-AUTH.md](EDGE-AUTH.md). This page covers what changes when the
Gateway Service is internet-facing: the public listener, the render guard,
CIDR allow-listing, rate limiting, WAF and DNS.

## Internet-facing defaults

On a cloud deploy the Gateway Service is internet-facing:
`envoyGateway.service.internetFacing` is true in `edge-aws.yaml`, `edge-gcp.yaml`
and `edge-azure.yaml`, because each cloud makes a LoadBalancer public unless told
otherwise.

**Public web access is default-deny, per exposure group.** A route's class puts
it in a group: dfe-ui, HyperDX and the engine's browser families are `console`;
the engine's CLI families, `/openapi.json` and SCIM are `api`; the admin UIs are
`admin`. Every route of a group, on its internal name and its public one, is
refused at Envoy unless the client address is on `exposure.groups.<group>.allow`,
and the group's `deny` list is refused first.

| `exposure.groups.<group>.allow` | The group's routes | Said by |
|---|---|---|
| empty | 403 to every address | NOTES, `dfe-ops access-summary` |
| ranges | the ranges, and `exposure.selfRanges` | NOTES |
| a `/0` of either family | every address | a WARNING in both |

Every web route the chart renders carries its own SecurityPolicy with its group's
rule; the OIDC ones AND it with their group check. The Gateway-wide policy,
`dfe-edge-fence`, refuses every address, so it holds only a route another
namespace attaches. Ingest routes (`otel`, `receiver`) carry an explicit allow,
since they are not web surfaces, and so does the `:80` redirect, which serves
nothing but a 301 to the fenced https side.

One Service carries every listener, so the load balancer cannot tell the groups
apart: its source ranges are the union of the enabled web groups' allow lists,
`exposure.selfRanges` and `envoyGateway.service.loadBalancerSourceRanges`. A group
that admits every address leaves it open. A sender to an ingest route goes in
`envoyGateway.service.loadBalancerSourceRanges`. The Envoy Service carries the
state as `dfe.hyperi.io/edge-fence: console=<state>,api=<state>,admin=<state>`.

`exposure.selfRanges` is the cluster's own NAT egress, which the aws root writes
onto the cluster secret. It is a fact, never an allow list: the deployer's lists
live in the deploy repo's `infra/common.yaml`, where the engine can change them at
runtime.

`exposure.groups.admin.enabled` defaults off on the same three flavours, and
`argocd/values/aws.yaml` sets `oidc.enabled: true` -- Argo CD, Kafbat, Forgejo,
the links page and Cruise Control all stay off the public load balancer until an
operator's overlay turns the group on. HyperDX is class product, beside the
console that frames it, so the switch never reaches it. The chart's render guard
(`envoy-gateway-config.validateUi`) refuses the admin group on an internet-facing
Service with an empty `exposure.groups.admin.allow`, unless `allowPublic: true`,
and refuses `allowPublic` without an edge OIDC provider (`oidc.enabled` and an
`oidc.providers` entry).

## Upgrading a cloud deployment

- Set `exposure.groups.console.allow` (and `api.allow`, `admin.allow` as needed) in the deploy repo's `infra/common.yaml` before upgrading. An internet-facing Gateway with an empty list answers 403 on that group's routes from the first sync.
- `ui.allowed_cidrs`, `exposure.infraUisExternal` and `ui.public.<admin UI>` are retired and refuse to render, each naming its replacement.
- The trusted ranges a deployment already runs with live on its Argo CD cluster secret, in `dfe.hyperi.io/edge_trusted_proxy_cidrs`, and stay trusted until `bootstrap.sh` runs again. Only bootstrap writes that annotation, and `dfe-ops refresh` and `dfe-ops upgrade` leave it alone. Re-run bootstrap after upgrading, or remove the annotation, so a load balancer's subnets stop being believed.

## The client address the fence judges

The fence judges the TCP peer. No listener believes `X-Forwarded-For` unless `ui.trusted_proxy_cidrs` names the hops in front, so neither a client nor a host beside the load balancer, such as the tunnel forwarder in an AWS public subnet, can claim a listed address. With no client-IP detection, Envoy Gateway v1.9.2 sets `use_remote_address` and trusts no hop (`internal/xds/translator/listener.go`).

| Flavour | Load balancer | Envoy's TCP peer |
|---|---|---|
| AWS | NLB with IP targets and `preserve_client_ip.enabled=true` (`edge-aws.yaml`) | the client |
| GCP | GKE's passthrough Network Load Balancer, `externalTrafficPolicy: Local` (Envoy Gateway's default) | the client |
| Azure | Azure Load Balancer, `externalTrafficPolicy: Local` | the client |
| On-prem | MetalLB or another L4 pool, `externalTrafficPolicy: Local` | the client, or a node address where the pool SNATs |

A CDN in front of the public hostnames writes the client into `X-Forwarded-For` and connects from its own ranges. Name those ranges in `ui.trusted_proxy_cidrs` (the dial's `edge.product.trusted_proxy_cidrs`). The public listeners then read the header right to left when the peer is inside the list and take the first address outside it, while every other connection is judged by its own address. The internal listener never reads the header. List only a proxy that writes the header itself: never an L4 load balancer, and never a subnet other hosts share.

## Public hostnames on a cloud deploy

Everything DFE uses internally is private. The UIs are the exception,
because their users sit outside the VPC, and the gateway chart's `ui:` values
expose them. Nothing in it acts until `ui.public_domain` (the dial's
`edge.product.domain`, itself the deployment's `dns.public_zone`) names a
delegated public zone.

The keys below are the CHART's. The dial states the same decisions under
`edge.product` and `edge.admin_uis` (`deployment.example.yaml`), where
`rate_limit`, `waf` and `tls` are these values verbatim and the rest is the
dial's own spelling.

| Key | Default | What it decides |
|---|---|---|
| `ui.public_domain` | `""` | The public zone the UIs answer on. Empty renders no public half. |
| `ui.public.<name>` | `dfe_ui: true`, `hyperdx: false` | Which product UIs get a public hostname. |
| `exposure.groups.admin.tools.<route>` | every tool `false` | Which admin UIs get a public hostname, once the admin group is on. |
| `exposure.groups.<group>.allow`, `.deny` | `[]` (nobody, internet-facing) | Who may reach the group's routes, at Envoy AND, as a union, at the load balancer. |
| `ui.trusted_proxy_cidrs` | `""` (no forwarded header believed) | A CDN or other L7 proxy in front of the public hostnames, whose `X-Forwarded-For` names the client. |
| `ui.rate_limit` | on, 300/Minute, `local` | The wall a credential-stuffing run hits before the login: every public route, and every internal-name web route on an internet-facing Service. |
| `envoyGateway.connectionLimit` | on, 10000 | Concurrent connections each listener takes per proxy replica. |
| `ui.waf.mode` | `none` | Only `none` renders. |
| `ui.tls` | `"1.2"`, HSTS on | The public TLS floor and `Strict-Transport-Security`. |

A public UI gets its OWN listener, its own publicly trusted certificate and
its own HTTPRoute `<route>-public` -- not a second hostname on the internal
route, because the policies that make a public route safe must not reach the
internal one. Internal routes keep the internal CA; the public hostnames
hold a Let's Encrypt certificate issued by DNS-01 on the public zone through
cert-manager's Pod Identity role (`tls.public`).

## What fails the render

These fail the render rather than shipping a weaker edge:

- **A public UI with no authentication.** Edge OIDC (`edgePolicy`, or
  `oidc.targetRoutes` naming the route, with a provider configured) counts,
  as do the three app-side schemes above -- dfe-ui's NextAuth, Kafbat's own
  OIDC, HyperDX's `dfe_token` cookie. Nothing else does.
- **The receiver route on an internet-facing Service with no caller auth.**
  The chart cannot read the receiver's `config.server.auth.mode`, so
  `routes.receiver.authMode` restates it, and only `header` or `bearer`
  renders. Envoy terminates TLS, so `mtls` never sees a client certificate.
- **`internetFacing: false` on a load balancer the cloud makes public.** The
  key is declared, not derived, so `false` is refused beside
  `service.beta.kubernetes.io/aws-load-balancer-scheme: internet-facing`, and on
  GCP or Azure (`cloud` or `global.cloud`) on a LoadBalancer Service with no
  `networking.gke.io/load-balancer-type: Internal` or
  `service.beta.kubernetes.io/azure-load-balancer-internal: "true"`.
- **An entry in any group list, `selfRanges` or `trusted_proxy_cidrs` that is
  not a CIDR range**, such as a bare address. Envoy Gateway and the load
  balancer would refuse it at admission, failing the whole gateway sync.
- **The admin group on an internet-facing Service with no allow list**, unless
  `allowPublic` is true with an edge OIDC provider.
- **A retired key**: `ui.allowed_cidrs`, `exposure.infraUisExternal` or
  `ui.public.<admin UI>`, each refused with its replacement named.
- **A `/0` of either family in `trusted_proxy_cidrs`.** Every peer is then
  trusted, so Envoy takes the client from the leftmost `X-Forwarded-For`
  entry, which the caller writes. `render_dial.py`, `bootstrap.sh` and the aws
  root refuse it too.
- **`rate_limit.scope: global`**, which needs a Redis backend named in the
  EnvoyGateway install config.
- **`waf.mode` other than `none`.** A WAF terminates TLS above Envoy, which
  moves the public certificate to the cloud's certificate store.
- **A `ui.public.<name>` with no route**, rather than the flag doing nothing.

On AWS the load-balancer half of the filter is `loadBalancerSourceRanges`,
which the AWS Load Balancer Controller turns into the NLB's frontend
security group. That group can only be attached at creation, so the
controller must own the Service from the start -- see the annotations and
their reasons in `argocd/values/edge-aws.yaml`. The ranges fence the WHOLE front
door, ingest included: one Service carries every listener.

The engine answers on two routes on its one hostname: `dfe-engine` carries the
browser families (`routes.dfeEngine.browserFamilies`) for the console group, and
`dfe-engine-cli` carries the CLI families, `/openapi.json`, the Swagger pages
where the engine serves them and, with `exposure.groups.api.scim`, SCIM, for the
api group. The edge OIDC login fronts `dfe-engine` alone, because the CLI cannot
follow an interactive redirect; the engine authenticates every call itself.
