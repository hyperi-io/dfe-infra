# Edge exposure and authentication -- cloud specifics

The model, the identity plane and on-prem behaviour:
[EDGE-AUTH.md](EDGE-AUTH.md). This page covers what changes when the
Gateway Service is internet-facing: the public listener, the render guard,
CIDR allow-listing, rate limiting, WAF and DNS.

## Internet-facing defaults

On a cloud deploy the Gateway Service is internet-facing, so
`exposure.infraUisExternal` defaults off rather than on.
`argocd/values/aws.yaml` sets `exposure.infraUisExternal: false` and
`oidc.enabled: true` -- Argo CD, Kafbat, HyperDX, Forgejo, the links page and
Cruise Control all stay off the public NLB until an operator's overlay opts
one back on. The chart's own render guard (`envoy-gateway-config.validateUi`)
fails the render if a deploy flips the switch back on for an internet-facing
Service with `oidc.enabled` false and `ui.allowed_cidrs` empty -- so
re-exposing an admin UI on a public load balancer is always a deliberate
choice with either OIDC or a CIDR fence behind it, never a one-line flip
that quietly ships with no edge auth.

## Public hostnames on a cloud deploy

Everything DFE uses internally is private. The UIs are the exception,
because their users sit outside the VPC, and the `ui:` block exposes them.
Nothing in it acts until `ui.public_domain` (the dial's `dns.public_zone`)
names a delegated public zone.

| Key | Default | What it decides |
|---|---|---|
| `ui.public_domain` | `""` | The public zone the UIs answer on. Empty renders no public half. |
| `ui.public.<name>` | `dfe_ui: true`, admin UIs `false` | Which UIs get a public hostname. |
| `ui.allowed_cidrs` | `""` (any) | Who may reach one, at Envoy AND at the load balancer. |
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
their reasons in `argocd/values/aws.yaml`. The ranges fence the WHOLE front
door, ingest included: one Service carries every listener.
