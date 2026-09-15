# Edge OIDC through the gateway

DFE has two ways to authenticate a person. Local auth is the default and needs
nothing: dfe-engine holds the accounts and signs its own tokens. The other is
edge OIDC, where Envoy Gateway takes the login, verifies the token, and hands
dfe-engine an already-authenticated identity in `X-Oidc-*` headers.

This page is about the second one, and about the parts of it that Envoy Gateway
cannot do today. Read it before turning `jwtAuthn.enabled` on.

## The values that turn OIDC on

There is no single on/off switch, and deliberately no `auth.oidcEnabled`: the
engine has no OIDC boolean at all, it reads whatever provider YAML is under
`<config.mountPath>/auth/oidc-providers`. Four values say four separate things.

| chart | value | what it decides |
|---|---|---|
| dfe-engine | `authConfig.providersConfigMap` | which ConfigMap of provider definitions is seeded into the engine's auth directory |
| dfe-engine | `oidc.enabled` + `oidc.providers` | which client credentials are mounted as env from which Secrets |
| dfe-engine | `auth.trustProxyHeaders` | whether the engine believes `X-Oidc-*` on an inbound request |
| envoy-gateway-config | `oidc.enabled` + `oidc.providers` + `oidc.targetRoutes` | which routes get an OIDC SecurityPolicy, and against which IdP |

## A private-CA IdP is not discoverable

An IdP the deployment hosts for itself is the common shape, because the gateway
chart issues the whole wildcard from `dfe-internal-ca`. Point a SecurityPolicy
at one with `provider.issuer` alone and it is rejected at admission:

```
OIDC: Get "https://auth.example.com/.well-known/openid-configuration":
tls: failed to verify certificate: x509: certificate signed by unknown authority
```

That fetch is done by Envoy Gateway's CONTROL PLANE, against the system trust
store. The SecurityPolicy CRD has no field anywhere that takes a CA bundle:
`provider.backendSettings` is cluster settings only -- timeouts, retries, health
checks, load balancing -- with no TLS block in it.

So skip discovery. Name both endpoints, and send the token exchange to the IdP's
Service over the pod network:

```yaml
oidc:
  enabled: true
  providers:
    - name: dex
      issuerUrl: https://auth.example.com      # must equal the token `iss`
      clientId: dfe
      authorizationEndpoint: https://auth.example.com/auth
      tokenEndpoint: https://auth.example.com/token
      backendRefs:
        - name: dex
          namespace: dfe-local
          port: 5556
```

The authorization endpoint stays the public URL -- the browser goes there, not
Envoy. The token endpoint is called by Envoy, which is why it needs the
`backendRefs` alongside it. A `backendRefs` entry outside the target route's
namespace needs a ReferenceGrant in the IdP's namespace.

If the IdP Service itself speaks HTTPS with a private CA, the CA goes on a
BackendTLSPolicy targeting that Service, not on the SecurityPolicy.

## `X-Oidc-Groups` cannot be minted

`SecurityPolicy.spec.jwt.providers[].claimToHeaders` states its own limit:

> The claim must be of type; string, int, double, bool. Array type claims are
> not supported

`groups` is an array in every OIDC provider worth testing against. There is no
Envoy Gateway feature that flattens one, and inventing a workaround out of the
patch policy does not change what the filter accepts.

What that means in practice, proven on a live edge login: the engine receives
`X-Oidc-Subject`, authenticates the user, and answers

```json
{"user_id":"CgQ2MDAxEgRsZGFw","roles":[],"permissions":[],"groups":[]}
```

Authenticated, and authorized for nothing. `_resolve_group_grants` maps the
groups header to roles, the header never arrives, so no role is resolved and
every authorization check fails.

The way out is the engine reading the ID token itself rather than trusting a
header the gateway cannot produce. Until that exists, edge OIDC gives you
authentication and no authorization.

Two more things in the same path that do not meet at the Envoy Gateway this
repo pins (`versions.yaml`, `envoy-gateway`):

- The `EnvoyPatchPolicy`'s `jwt_authn` filter takes the token from an
  `Authorization: Bearer` header. An interactive OIDC login leaves the browser
  holding a cookie, not a bearer header.
- `auth.trustProxyHeaders` is a per-ENGINE flag, while the header stripping that
  makes `X-Oidc-*` trustworthy is per-gateway-LISTENER. One engine deployment
  serving both a policy-fronted route and a plain one cannot trust the headers on
  one and not the other, so setting the flag makes them spoofable everywhere that
  engine is reachable.

## Reaching your own gateway from inside the cluster

A pod that has to use the deployment's public hostname rather than a Service
name -- the engine fetching an IdP's discovery document, for one -- needs two
things that are easy to miss.

**Egress on the listener port.** `network-policies` ships
`allow-gateway-egress` for this. The baseline policy permits 443 to anywhere,
which reads as "pods may reach the gateway", and they cannot: the Envoy Service
maps 443 to container port 10443, NetworkPolicy is evaluated after DNAT, so the
packet is judged against 10443 and dropped. Refused-vs-timeout is the tell --
443 straight at the proxy pod is refused by the kernel, 10443 never arrives.

**A name that resolves, to an address that hairpins.** This one the deployment
owns, not DFE. The deployment's own hostnames have to resolve in-cluster, and
they have to resolve to an address a pod can actually connect to:

- With a cloud DNS provider configured, external-dns publishes the records and
  in-cluster resolution follows the public ones.
- On a private domain with no cloud DNS, the cluster needs a split-horizon
  answer for the deployment's domain. That is a cluster-DNS change -- a CoreDNS
  zone or drop-in, whose mechanism differs per distribution -- so it belongs in
  the deployment's own infrastructure config, not in a DFE chart.
- Answer with the gateway Service's ClusterIP, NOT its LoadBalancer VIP. Many
  on-prem load balancers do not hairpin, so a pod that resolves the VIP connects
  and then times out. Set `envoyGateway.service` deliberately if the deployment
  needs a stable name for that record.

SNI and Host stay the public name either way, so Envoy routes on the HTTPRoute
hostname and serves the wildcard certificate as normal.

## Related

- [index.md](index.md) - the deploy layers and the values cascade
- `helm/edge/gateway/values.yaml` - the `oidc` and `jwtAuthn` keys
- `helm/charts/dfe-engine/values.yaml` - the `auth`, `oidc` and `authConfig` keys
