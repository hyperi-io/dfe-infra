# Shared values

`argocd/values/common.yaml` and its helpers hold the facts that more than one chart reads. The files carry a pointer here and little else, so this page is the reasoning behind the keys that are not obvious from the key. The merge order is in [README.md](README.md#tiers-and-the-values-cascade). Cloud, edge and tier files are in [values-clouds-edge.md](values-clouds-edge.md) and [values-tiers-apps.md](values-tiers-apps.md).

## Shared facts

`common.yaml` defines every fact that more than one chart reads: hostnames, the telemetry destination, admin groups, the exposure dial, network ranges and the ClickHouse and Kafka endpoints. Defining one anywhere else is config sprawl. Pins live in `versions.yaml`.

- `hostnames` names each surface `{hostname}.{domain}` by role, not by product. `dfe` serves dfe-ui at `/` and the engine API at `/api/v1` on one hostname. `receiver` is read with `exposure.groups.ingest.door: gateway`. `vpn` is culvert's server certificate CN.
- `global.registry` is written by bootstrap from `DFE_REGISTRY` onto the `dfe.hyperi.io/registry` cluster-secret annotation, and the layer 2 appsets pass it back as a parameter. No values file sets it.
- `adminGroups` are the OIDC groups that administer the ops surfaces. The gateway admits exactly these to every infra route, and Kafbat grants them its admin role unless an overlay names one. Argo CD keeps a separate policy in `bootstrap/argocd_login.py`, and `scripts/tests/test_argocd_login.py` fails when the two differ. An empty list fails to render.
- `exposure` holds the product defaults, where an empty allow list means no fence. Each `edge-<flavour>.yaml` has an internet-facing gateway, so there an empty list rejects every address and admin is off. `helm/edge/gateway/values.yaml` documents each field, and [edge.md](edge.md) and [ingest-edge.md](../ingest-edge.md) the doors. `deny` reaches the gateway routes, not the receiver's load balancer. `admin.tools` is the canonical admin list, one key per class-infra gateway route. `selfRanges` is written from the cluster secret, never by hand.
- `operatorNamespaces` drives network-policies' data-namespace ingress and the collector's operator scrapes. A name that matches no namespace fails silently, because a `namespaceSelector` on a missing namespace matches nothing. The names must agree with the destinations in `argocd/appsets/layer-scale.yaml`.
- `nodeScheduling` and `nodeSelector` plus `tolerations` are one placement under two key sets, the second being what the scalo-service thin charts read. `scripts/tests/test_apps_common.py` checks that a file sets them equal.
- `ingestBearer` and `alertDestinations` name Secrets that dfe-engine writes into, while it keeps what it mints on a volume, which no receiver or hunt-runner pod can mount. dfe-extras renders each empty beside the engine with a Role granting the engine get and update on it alone.
- `api.docsEnabled` is `"true"`, `"false"` or empty. Empty serves Swagger UI and ReDoc in development environments (dev, development, local, test, ci). Both pages load scripts from a public CDN into the engine's origin.

## Telemetry

`telemetry.mode` picks where every app sends its OTLP.

- `hyperdx` (default) pushes to the stack's collector gateway, which writes ClickHouse's otel tables itself, so monitoring does not depend on the receiver or loader.
- `receiver` pushes through the dfe-receiver ingest path.
- `external` pushes to `externalEndpoint`.
- `prometheus` pushes nothing and exposes `/metrics` with scrape annotations.

An empty `hyperdxEndpoint` derives `{project}-otel-collector-gateway` in `telemetry.collectorNamespace` on 4317. An empty `receiverEndpoint` derives the receiver's OTLP/gRPC listener, which it binds while `config.otlp.enabled` is true. `apps/_common.yaml` resolves the endpoint, because scalo-service sets no OTLP variable for an empty `otel.endpoint` and the app would keep its default. `scripts/tests/test_apps_common.py` holds every mode to its known endpoint.

`otel.endpoint` overrides the resolved endpoint. It needs a scheme (`https://host:4317`), because it is exported as written.

`otel.ingress` is OTLP ingest from outside the cluster on `otel.{domain}`, off because the stack's telemetry reaches the collector on its Service. The gateway renders the route and the collector the bearer-token receiver behind it, so a deployment turns it on in the deploy repo's `infra/common.yaml`. There is no unauthenticated mode: with `auth.remoteKey` empty both charts fail to render.

Every service boots with an opt-out release check. It is one POST of product, version, OS, architecture and an install id (a one-way UUIDv5, deployment names never sent) to the HyperI releases endpoint. Unset `versionCheck` leaves the apps' default, which is on. Set it fleet-wide in `common.yaml` or per app in the overlay. `enabled: false` is the full opt-out for an air gap, `sendInstanceId: false` strips the id and keeps the check, and `apiUrl` points it at a mirror.

## Kafka and ClickHouse endpoints

- `kafka.bootstrapServers` defaults to the single tier's broker Service. The cluster tier's broker is operator-owned and named differently, so `profile-scale.yaml` overrides it.
- `kafka.securityProtocol` is `SASL_PLAINTEXT` because the shipped broker authenticates with SCRAM over its plain listener. A TLS listener moves it to `SASL_SSL` with `bootstrapServers`. The SASL mechanism derives from `kafka.provider` (`dfe-common.kafkaSaslMechanism`), so there is no `sasl:` block.
- `user.password.extraAppNamespaces` is read by the kafka chart, which projects the SCRAM credential into those namespaces. The collector's `kafka_metrics` receiver authenticates as the same user, so `otel` belongs there, or the gateway pod cannot resolve `dfe-kafka-user` and never starts.
- `clickhouse.host` defaults to the single-mode Service. Cluster mode names the operator's headless Service, and external mode takes the supplied endpoint. `nativePort` serves clients on the native protocol, which dfe-loader defaults to.
- `clickhouse.user: default` is the admin account dfe-engine uses to create every other user, and the one credential the deploy layer owns. It is `default` because that is the one account the ClickHouse operator can password from a Secret, and what docker and single mode start with. The name and Secret match clickhouse-cluster's `users.admin`.
- `clickhouse.serviceUsers` are each app's user. dfe-extras mints the password into a Secret beside the engine, the engine creates the user, and the app connects with it ([clickhouse-identity.md](../clickhouse-identity.md)).

## ClickHouse TLS

`clickhouse.tls` is HTTPS between the apps and ClickHouse. clickhouse-cluster serves it, and dfe-engine, dfe-loader and HyperDX dial it from this one block. Each applies one rule: on when `enabled` and there is a CA to sign with, meaning an `issuerRef` or the edge module's internal CA. No CA means HTTP, so a cluster with the edge module off and no `issuerRef` never waits on a Certificate.

- `required: false` keeps 8123 and 9000 open beside 8443 and 9440, for smoke tests that dial plaintext.
- `verify: false` is for a server whose certificate cannot be verified. dfe-loader cannot skip verification and fails to render with it.
- `issuerRef` is an explicit issuer (estate PKI, AWS Private CA, a corporate CA), set in the deploy repo's `infra/common.yaml`. It turns TLS on with or without the edge module and must sign for in-cluster Service names, which public ACME cannot. Empty signs with the internal CA.
- `internalCA.present` comes from the appsets, off the cluster's `dfe.hyperi.io/edge` label, which decides whether the gateway deploys. The `true` in the file is bootstrap's default of edge on, for a render outside Argo.
- `ca` is where a client reads the signing CA in the client's namespace. A corporate bundle goes in `configMapName`, and both empty trusts the image's system roots.

## Network ranges

`networkModel` holds the ranges the CNI and kube-proxy hand out, as the cluster was installed. A deployment on different ranges changes them here and nowhere else. network-policies carves them out of every internet allow, so a compromised pod cannot reach pod or service space, and culvert pushes the service range as the route a tunnel client may take. A cloud root that knows its ranges writes them onto the cluster secret, and `layer2-platform.yaml` lays them over these.

AWS carves out the link-local range and nothing else. Pods take VPC addresses from the private subnets that MSK brokers and PrivateLink endpoints share, so carving the pod range cuts every in-VPC backend off its clients. The service range is left alone because the VPC CNI policy agent turns an `except` into a deny over every other rule on the pod, which cuts it off kube-dns (aws/aws-network-policy-agent#326).

`vpn.clientCIDR` is the CGNAT range (RFC 6598) reserved for tunnel clients and never for pods or services, so an appliance's LAN and the cluster cannot collide with it. The chart carves one /24 per tunnel out of it.

## Mesh pool balancer

A Kubernetes Service balances per connection and gRPC holds one connection open, so a sender pins itself to one pod of the pool it dials. `mesh.enabled` puts a Gateway API listener in front of every pool, and the proxy spreads each request across the pool. The mesh profile alone turns it on: single-replica tiers have nothing to spread, and brokered tiers balance through the consumer group.

- `gateway.className` names the implementation. `dfe-envoy` is the in-cluster Envoy Gateway the deployment runs at its edge, and a cloud's Gateway API class swaps in where the class name is the whole difference.
- `gateway.namespace` is where the Gateway and its proxy pods live. Each pool alias is a Service there that selects those pods, so it is the namespace half of every pool's mesh hostname. `proxySelector` uses Envoy Gateway's ownership labels, and another implementation supplies its equivalents.
- `routePolicy` is each pool's Envoy Gateway BackendTrafficPolicy (`dfe-common.poolListener`). Another implementation sets `enabled: false` and brings an equivalent.
- `requestTimeout` stays at or above the sender's send deadline. The next stage holds a request until its ack is released, so a shorter timeout cuts a request the sender still waits on.
- `retry` fires on connect-failure and resource-exhausted, the two failures where no pod took the request. The trigger list is fixed in the helper, because widening it re-delivers batches.
- `outlier` is passive detection. Envoy maps a gRPC unavailable to 503, so a pod that answers it `consecutive5xxErrors` times running leaves the rotation, with at most `maxEjectionPercent` of the pool out. Resource-exhausted maps to 429 and ejects nothing.

