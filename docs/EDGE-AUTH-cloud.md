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

**Public web access is default-deny.** Every web route on that Service --
dfe-ui, HyperDX, the engine API and every admin UI, on the internal names and the
public ones -- is refused at Envoy unless the client address is on
`ui.allowed_cidrs`. With the list empty, every address gets a 403.

| `ui.allowed_cidrs` | Web routes | Load balancer | Said by |
|---|---|---|---|
| empty | 403 to every address | open, so ingest still reaches its own auth | NOTES, `bootstrap.sh`, `dfe-ops access-summary`, `render_dial.py` |
| ranges | the ranges only | the ranges, plus `envoyGateway.service.loadBalancerSourceRanges` | NOTES |
| a `/0` of either family | every address | every address | a WARNING in all four |

One Gateway-wide SecurityPolicy, `dfe-edge-fence`, carries the rule. Envoy Gateway
applies only the most specific SecurityPolicy to a route, so every route-level
policy the chart renders restates it. The OIDC ones AND it with their group
check. Ingest routes (`otel`, `receiver`) carry an explicit allow, since they are
not web surfaces. A route another namespace attaches inherits the deny.

The load balancer's half fences ingest too. A sender outside the web list goes in
`envoyGateway.service.loadBalancerSourceRanges`, and Envoy still holds the web
routes to the list. The Envoy Service carries the state as
`dfe.hyperi.io/edge-fence: deny | listed | allow-all`.

`exposure.infraUisExternal` defaults off on the same three flavours, and
`argocd/values/aws.yaml` sets `oidc.enabled: true` -- Argo CD, Kafbat,
Forgejo, the links page and Cruise Control all stay off the public load balancer
until an operator's overlay opts one back on. HyperDX is class product, beside
the console that frames it, so the switch never reaches it. The chart's own render guard (`envoy-gateway-config.validateUi`)
fails the render if a deploy flips the switch back on for an internet-facing
Service with no edge OIDC provider (`oidc.enabled` and an `oidc.providers`
entry) and `ui.allowed_cidrs` empty -- so
re-exposing an admin UI on a public load balancer is always a deliberate
choice with either OIDC or a CIDR fence behind it, never a one-line flip
that quietly ships with no edge auth.

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
| `ui.public.<name>` | `dfe_ui: true`, admin UIs `false` | Which UIs get a public hostname. |
| `ui.allowed_cidrs` | `""` (nobody, internet-facing) | Who may reach a web route, at Envoy AND at the load balancer. |
| `ui.trusted_proxy_cidrs` | `""` | Proxy ranges Envoy may believe about the client address. |
| `ui.rate_limit` | on, 300/Minute, `local` | The wall a credential-stuffing run hits before the login. |
| `ui.waf.mode` | `none` | Only `none` renders. |
| `ui.tls` | `"1.2"`, HSTS on | The public TLS floor and `Strict-Transport-Security`. |

A public UI gets its OWN listener, its own publicly trusted certificate and
its own HTTPRoute `<route>-public` -- not a second hostname on the internal
route, because the policies that make a public route safe must not reach the
internal one. Internal routes keep the internal CA; the public hostnames
hold a Let's Encrypt certificate issued by DNS-01 on the public zone through
cert-manager's Pod Identity role (`tls.public`).

## What fails the render

Five things fail the render rather than shipping a weaker edge:

- **A public UI with no authentication.** Edge OIDC (`edgePolicy`, or
  `oidc.targetRoutes` naming the route, with a provider configured) counts,
  as do the three app-side schemes above -- dfe-ui's NextAuth, Kafbat's own
  OIDC, HyperDX's `dfe_token` cookie. Nothing else does.
- **`allowed_cidrs` with no `trusted_proxy_cidrs`.** Envoy would take the
  client address from the leftmost `X-Forwarded-For` entry, which the caller
  writes.
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
