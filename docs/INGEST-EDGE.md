# The ingest edge

How data gets INTO a DFE deployment from outside the cluster, and who is
allowed to send it. The web plane is a separate door with a separate model
-- see [EDGE-AUTH.md](EDGE-AUTH.md) for the UIs and their edge RBAC.

`dfe-receiver` is a single-homed airlock: it takes traffic from outside and
produces to local Kafka, which never faces the network. Its chart owns the
door in `exposure.mode`.

| mode | What renders | Client CIDR control |
|---|---|---|
| `public` (default) | one LoadBalancer per protocol family carrying the `exposed` listeners, plus a NetworkPolicy opening those ports | `exposure.public.loadBalancerSourceRanges` |
| `internal` | ClusterIP only; the door is the Gateway HTTPRoute `routes.receiver` in envoy-gateway-config, on `receiver.{domain}` | the Gateway's own edge |
| `vpn` | ClusterIP only; the door is the edge-fleet tunnel, and the ingest NetworkPolicy admits the VPN pods rather than the world | the VPN's own LoadBalancer, plus per-client PKI or OIDC |

## The default is open

**A default deploy is internet-facing on the ingest ports, and
`loadBalancerSourceRanges` starts EMPTY, which means `0.0.0.0/0` -- every
source address on earth.** It is not "unset", it is "everyone". Set it to
the CIDRs that may send, or put an edge in front (cloud LB, WAF, mTLS, API
gateway) that does.

Nothing else is holding that door. The receiver's `server.auth.mode`
defaults to `none` in the app and in the engine's config model, and the
chart ships `config: {}`, so a deployment that has not configured auth
accepts unauthenticated posts on `/ingest`. Body size, request timeout and
the per-IP rate limiter are the only other limits. A public deployment
needs `loadBalancerSourceRanges`, or `server.auth.mode: bearer` with tokens
in the engine's overlay, or both.

The two listeners a fresh deploy exposes are `http` 8080 (the documented
core data path -- `POST /ingest` -- plus OTLP/HTTP) and `grpc` 8443
(OTLP/gRPC). `/livez` and `/readyz` share the http listener, so exposing
ingest exposes both probe paths.

Client-CIDR filtering is the LoadBalancer's job, not the NetworkPolicy's:
under the default `externalTrafficPolicy` kube-proxy SNATs external traffic
to a node address, so an `ipBlock` would match the node rather than the
client and drop traffic the LB had already allowed. The chart's ingest
NetworkPolicy therefore opens the exposed ports and leaves the allow-list
to the LB.

## Internal mode routes HTTP only

Internal mode routes only the `http` listener, because an HTTPRoute carries
HTTP. A gRPC, lumberjack, netflow or syslog listener marked `exposed: true`
in internal mode has no path at all, so the chart **fails the render** and
names the listener rather than deploying something that silently receives
nothing. The two ways out are public mode, or a dedicated Gateway listener
with its own TCPRoute/UDPRoute.

Pair the two values deliberately: `routes.receiver.enabled` ships `false`
because the receiver ships `public`, where it has its own LoadBalancer and
the route would be a second door. A deployment on `internal` flips both.

`vpn` mode is not caught by that guard, and deliberately so: a tunnel
delivers a client onto the pod network and it dials the ClusterIP directly,
so every exposed listener is reachable there without a Gateway route at all.
See [deployment/edge-vpn.md](deployment/edge-vpn.md).
