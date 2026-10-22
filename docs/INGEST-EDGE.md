# The ingest edge

How data gets INTO a DFE deployment from outside the cluster, and who is
allowed to send it. The web plane is a separate door with a separate model
-- see [EDGE-AUTH.md](EDGE-AUTH.md) for the UIs and their edge RBAC.

`dfe-receiver` is a single-homed airlock: it takes traffic from outside and
produces to local Kafka, which never faces the network. Its door is the ingest
exposure group, `exposure.groups.ingest` in `argocd/values/common.yaml`, which a
deployment sets in its deploy repo's `infra/common.yaml`. The gateway chart reads
the same block, so one value decides both charts.

| `door` | What renders | Client CIDR control |
|---|---|---|
| `loadbalancer` (cloud default) | while `allow` names someone: one LoadBalancer per protocol family carrying every public port of the receiver's contract that its config turns on, plus a NetworkPolicy admitting the same ports. With `allow` empty: nothing, ClusterIP only | `allow` as the load balancers' `loadBalancerSourceRanges` |
| `gateway` (on-prem default) | ClusterIP only; the door is the gateway route `routes.receiver` on `receiver.{domain}` | `allow` and `deny` on the route's own SecurityPolicy, and `allow` joins the gateway load balancer's source ranges |
| `tunnel` | ClusterIP only; the door is the edge-fleet tunnel, and the ingest NetworkPolicy admits the tunnel pods, not the world, to the `listeners` marked `exposed` | the tunnel's own load balancer, plus per-client PKI or OIDC |

`enabled: false` renders no public door on any `door`, and takes the gateway's ingest routes (the receiver's and otel's) off too.

`exposure.public` carries the load balancer's shape: `annotations` and `loadBalancerClass` for the load balancer controller, `externalTrafficPolicy`, `serviceType`, and `loadBalancerIP`, which the cluster secret's `dfe.hyperi.io/receiver_address` overrides when set. Each cloud's file under `argocd/values/apps/dfe-receiver/` sets it: AWS Load Balancer Controller annotations (`external`, IP targets, internet-facing, client address preserved, backend security group rules managed) on AWS, and `externalTrafficPolicy: Local` on GCP and Azure, whose load balancers target nodes.

On AWS the TCP load balancer also takes the Elastic IPs the cloud root allocates, one per public subnet (`terraform/modules/edge/aws/receiver.tf`, the dial's `edge.aws.receiver_eips`), which reach the chart as `exposure.public.eipAllocations` and `exposure.public.subnets` through the cluster secret. The UDP load balancer, which renders only when a UDP listener is on, takes AWS-assigned addresses.

## Refused rather than ignored

- `exposure.mode` is retired: `public` is `door: loadbalancer`, `internal` is `door: gateway`, `vpn` is `door: tunnel`. A render that still sets it fails and names the replacement.
- `exposure.public.loadBalancerSourceRanges` is retired: the allow list is `exposure.groups.ingest.allow`.
- scalo-service's own `publicService.*` keys, because nothing builds the receiver's load balancer from them (`loadBalancerIP` is the appset's, carrying the receiver address).
- `allow` beside any `serviceType` but `LoadBalancer`, where Kubernetes ignores source ranges.
- An `aws-load-balancer-security-groups` annotation, which replaces the frontend security group the controller fills from `allow`, so the allow list would stop filtering.
- `deny` on `door: loadbalancer` (see below).

## Empty is closed, and a /0 is open

**On `door: loadbalancer` an empty `allow` renders no load balancer at all.** A cloud deploy has no public ingest until the deploy repo names the senders. A `/0` of either family admits every address on earth: it renders, marks each Service `dfe.hyperi.io/ingest-fence: allow-all`, and the chart's NOTES and `render_dial.py` warn. On an internet-facing gateway an empty `allow` refuses every address on the ingest routes; on-prem the gateway fences nothing while it is empty.

The allow list is not authentication. The engine writes each bearer listener's `auth` (`server`, `grpc`, `otlp`, `splunk_hec`, `prometheus_rw`), and a listener no ingest credential lists runs `mode: none`, accepting unauthenticated posts from every address `allow` names and landing them in the default org. Create an ingest credential listing the listener and the engine switches it to `mode: bearer`, reading the tokens from the deployment's secrets backend. A hand edit to that `auth` is drift the engine re-syncs.

An ingest credential minted through the engine reaches the receiver without an overlay. On an AWS engine store the receiver reads it from Secrets Manager. Everywhere else the engine writes each listener's tokens into the `dfe-ingest-bearer-tokens` Secret (`ingestBearer.secretName` in `argocd/values/common.yaml`), which dfe-extras renders empty with a Role granting the engine `get` and `update` on it alone, and the receiver mounts it read-only at `/var/run/secrets/dfe/ingest-bearer-tokens`. The engine compiles a 60 second bearer refresh, so a new, rotated or revoked token takes effect within about the kubelet's sync of that mount plus 60 seconds.

A fresh deploy exposes one listener, `http` 8080, the documented core data path (`POST /ingest`). `/livez` and `/readyz` share the http listener, so exposing ingest exposes both probe paths. Every other public listener in the receiver's contract opens with its own `config.<name>.enabled`, and on `door: loadbalancer` reaches the load balancer and the policy with it:

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

The load balancer door's NetworkPolicy admits each port the load balancers serve from any source and leaves the allow list to the load balancer, which sees the sender's own address: IP targets with the client address preserved on AWS, `externalTrafficPolicy: Local` on GCP and Azure. It renders from the same port list as the load balancers, in `extraObjects` of `argocd/values/apps/dfe-receiver/values.yaml`, and `exposure.networkPolicy.enabled: false` drops it. The tunnel door's policy of the same name is dfe-extras', on the `exposed` listeners.

## The deny list

`deny` refuses addresses ahead of `allow`. Cloud load balancer rules (an NLB security group, a VPC firewall, an NSG) are allow-only, so a deny list has to land behind them:

- On `door: gateway`, Envoy refuses `deny` on the receiver and otel routes before it checks `allow`.
- On `door: loadbalancer` the one place left is the receiver's own `server.ip_filter` in `denylist` mode. The receiver reads that from its config file, which no values layer can derive from the ingest group, so a `deny` on this door is refused at render rather than accepted and ignored. Narrow `allow` instead.

`server.ip_filter` does not reach the receiver's gRPC listeners in any case: tonic runs their accept loop, so gRPC and OTLP gRPC (4317) take no ip_filter and no per-IP rate limit (dfe-receiver `docs/HARDENING.md`). The load balancer's `allow` still applies to them, and so does their own `auth.mode`. <!-- doc-paths: ignore -->

## The gateway door routes HTTP only

`door: gateway` routes only the `http` listener, because an HTTPRoute carries HTTP. A gRPC, lumberjack, netflow or syslog listener marked `exposed: true` on this door has no path at all, so the chart **fails the render** and names the listener rather than deploying something that silently receives nothing. The two ways out are `door: loadbalancer`, or a dedicated Gateway listener with its own TCPRoute/UDPRoute.

The gateway renders the receiver route from the same `door`, so there is no second switch to pair with it. On an internet-facing gateway the route refuses to render unless `routes.receiver.authMode` restates a receiver `server.auth.mode` that checks every caller.

`door: tunnel` is not caught by that guard, and deliberately so: a tunnel delivers a client onto the pod network and it dials the ClusterIP directly, so every exposed listener is reachable there without a Gateway route at all. See [deployment/edge-vpn.md](deployment/edge-vpn.md).
