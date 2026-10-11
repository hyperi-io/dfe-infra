# The ingest edge

How data gets INTO a DFE deployment from outside the cluster, and who is
allowed to send it. The web plane is a separate door with a separate model
-- see [EDGE-AUTH.md](EDGE-AUTH.md) for the UIs and their edge RBAC.

`dfe-receiver` is a single-homed airlock: it takes traffic from outside and
produces to local Kafka, which never faces the network. Its chart owns the
door in `exposure.mode`.

| mode | What renders | Client CIDR control |
|---|---|---|
| `public` (default) | one LoadBalancer per protocol family carrying every public port of the receiver's contract that its config turns on, plus a NetworkPolicy admitting the same ports | `exposure.public.loadBalancerSourceRanges` |
| `internal` | ClusterIP only; the door is the Gateway HTTPRoute `routes.receiver` in envoy-gateway-config, on `receiver.{domain}` | the Gateway's own edge |
| `vpn` | ClusterIP only; the door is the edge-fleet tunnel, and the ingest NetworkPolicy admits the VPN pods, not the world, to the `listeners` marked `exposed` | the VPN's own LoadBalancer, plus per-client PKI or OIDC |

`exposure.public` also takes `annotations` and `loadBalancerClass` for the load balancer controller, `serviceType`, and `loadBalancerIP`, which the cluster secret's `dfe.hyperi.io/receiver_address` overrides when set. The render refuses scalo-service's own `publicService.*` keys on the receiver, because nothing builds its load balancer from them (`loadBalancerIP` is the appset's, carrying that address), and refuses `loadBalancerSourceRanges` beside any `serviceType` but `LoadBalancer`, where Kubernetes ignores it.

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

A fresh deploy exposes one listener, `http` 8080, the documented core data path (`POST /ingest`). `/livez` and `/readyz` share the http listener, so exposing ingest exposes both probe paths. Every other public listener in the receiver's contract opens with its own `config.<name>.enabled`, and in public mode reaches the load balancer and the policy with it:

| `<name>` | Ports |
|---|---|
| `otlp` | 4317 (gRPC), 4318 (HTTP) |
| `lumberjack` | 5044 |
| `splunk_hec` | 8088 |
| `prometheus_rw` | 9091 |
| `webhook` | 8090 |
| `syslog` | 514 TCP and UDP, 6514 TCP |
| `fluent` | 24224 |
| `gelf` | 12201 |
| `flow` | 2055, 4739 and 6343, all UDP |

Client-CIDR filtering is the LoadBalancer's job, not the NetworkPolicy's: under the default `externalTrafficPolicy` kube-proxy SNATs external traffic to a node address, so an `ipBlock` would match the node rather than the client and drop traffic the LB had already allowed. The public-mode ingest NetworkPolicy therefore admits each port the load balancers serve from any source and leaves the allow-list to the LB. It renders from the same port list as the load balancers, in `extraObjects` of `argocd/values/apps/dfe-receiver/values.yaml`, and `exposure.networkPolicy.enabled: false` drops it. vpn mode's policy of the same name is dfe-extras', on the `exposed` listeners.

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
