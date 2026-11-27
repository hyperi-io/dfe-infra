<!--
Project:   dfe-infra
File:      docs/threat-model.md
Purpose:   Exposure model for DFE risk analysis -- what an attacker can reach
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# DFE exposure model

**Most of this stack has no internet-facing and no user-facing exposure.** Three components do, and a fourth where a deployment turns the edge tunnel on. Everything else consumes from the broker or is reached from inside the deployment alone.

Read this before you assess a security finding, advisory or scanner warning in a DFE repo. Grade a finding by whether anything in DFE can reach the flawed code, as well as by its severity score.

## What is exposed

| Component | Exposure | Notes |
|---|---|---|
| **dfe-receiver** | **Internet / user facing** | The ingest edge. Takes arbitrary data from whoever can route to it. The primary attack surface. |
| **dfe-ui** | **User facing** | Browser-facing Next.js app, behind auth in every deployment that has an issuer. |
| **dfe-hyperdx** | **User facing, authenticated** | Search and dashboards embedded in dfe-ui. Verifies the engine's token on every request and queries ClickHouse as the one ClickHouse user the engine hands that session: one dfe-hyperdx team per ClickHouse identity, and a team holding any other user's connection refuses the session. Admin surfaces are engine-only. Its store holds every team's connection, the unrestricted platform reader's included, so a flaw that crosses the team fence reaches every org's data. Signed-in members reach it through the gateway on port 8080, from `exposure.groups.console.allow` alone, and every public route is rate-limited (`ui.rate_limit`, below). |
| The gateway's web routes | **Internet facing from each exposure group's allow list alone** | On every cloud flavour Envoy refuses dfe-ui, dfe-hyperdx and the engine's browser families to an address off `exposure.groups.console.allow`, the engine's CLI families and `/openapi.json` off `exposure.groups.api.allow`, and the admin UIs off `exposure.groups.admin.allow`. An EMPTY list refuses every address. The ingest routes (otel, and the receiver on the gateway door) answer to `exposure.groups.ingest.allow` the same way. Envoy parses an outsider's request before the check, so an Envoy advisory is reachable. |
| **culvert** | **Internet facing, opt-in** | The edge-fleet VPN (below). |
| dfe-fetcher | Outbound only | Reaches external APIs, but **it initiates** every connection. Its ingest port is off by default. |
| dfe-engine | CLI and API | Reachable by operators, not by the public. The API is authenticated. |
| dfe-loader, dfe-archiver, dfe-transform-* | Not exposed | Consume from the broker and write to a datastore. On the direct transport, dfe-loader, dfe-archiver, dfe-transform-vrl, dfe-transform-vector and dfe-transform-elastic each bind a plaintext, unauthenticated gRPC Push listener on a ClusterIP Service, port 6000 (the `push` port of the app's thin-chart Service). The namespace baseline NetworkPolicy (`dfe-ingress-policy`, `helm/charts/network-policies`) admits the gateway namespace and every DFE and otel namespace, so the listener is reachable from pods in those namespaces alone, never from outside the cluster. |
| ClickHouse, Kafka, the collector | Not exposed | Cluster-internal. On Kubernetes the apps reach ClickHouse over verified TLS (below). dfe-docker's `docs/operating.md` covers the Compose bindings. <!-- doc-paths: ignore --> |
| The DocumentDB store | Not exposed | dfe-hyperdx's state, in `cnpg`: the DocumentDB gateway on 10260 in front of PostgreSQL, in each instance pod. A ClusterIP Service follows the primary. dfe-hyperdx's login holds readWriteAnyDatabase and clusterAdmin, so it reads and writes every database in the store, and dfe-hyperdx alone reads its copy. dfe-hyperdx alone may open 10260, the instances alone open each other's 5432, and CloudNativePG alone opens 8000 (`documentdbAccess`, `helm/charts/network-policies`). The instance pods sit outside `data-ingress-policy` and take their whole ingress from `allow-documentdb-ingress`, so the gateway, collector and ClickHouse operator namespaces reach none of their ports. |
| The collector's OTLP ingress | **Off by default. When a deployment sets `otel.ingress.enabled`: reachable from outside the cluster, authenticated** | `otel.<domain>` on the gateway reaches a second OTLP/HTTP receiver that refuses any request without the bearer token from the deployment's secret store. The check is the collector's (`bearertokenauth`), so it holds however that port is reached. Before the token is checked, the collector's HTTP server and the extension parse an outsider's request: on such a deployment, grade an advisory in either as reachable. The in-cluster receiver on 4317/4318 stays unauthenticated and no route publishes it. |

## The gateway's trusted proxy ranges

No gateway listener believes `X-Forwarded-For` by default. The CIDR filter judges the TCP peer, which every flavour's load balancer delivers unchanged ([edge-auth-cloud.md](edge-auth-cloud.md#the-client-address-the-fence-judges)). So a forged header from a client, the tunnel forwarder in an AWS public subnet, or any other host beside the load balancer passes nothing.

`ui.trusted_proxy_cidrs` is the one way to change that. The public listeners (`https-public-*`) then read the header when the TCP peer is inside the list. Every host in a listed range can claim any client address, so list a CDN's ranges and nothing shared. Treat a deployment that lists anything else as fenced to that whole range.

The receiver route through the gateway is refused on an internet-facing Service unless `routes.receiver.authMode` says the receiver checks callers (`header` or `bearer`). The gateway can't read the receiver's values, so that key is what the operator declared and no more. If the two values disagree, grade the deployment by the receiver's.

## The edge tunnel

culvert is off unless a deployment asks for it. On-prem it's a public LoadBalancer on UDP 51820 (WireGuard) and 1194 (OpenVPN). On AWS it's a NodePort (31820, 31194) behind the tunnel forwarder's address or one the deployer brings. Empty source ranges mean every address. `openvpn-tcp` and `oauth2-udp` exist where `listeners` adds them, and nowhere else.

A client authenticates with a PKI certificate of its own, and with OIDC when `vpn.oidc.enabled`. Before that, an outsider's packets reach the VPN daemons, so grade an advisory in OpenVPN, WireGuard or the culvert image as reachable. The pod runs as uid 0 with `NET_ADMIN`. By design an authenticated client reaches the receiver's ingest port and cluster DNS on port 53, and no other port, service or client.

## In-cluster transport

A deployment that has a CA runs its in-cluster hops over TLS with verification on. One without stays on plain HTTP, because there's nothing to verify against. A Kubernetes deployment with the edge module on (bootstrap's default) has one: bootstrap.sh detects or installs cert-manager, and the gateway chart's `dfe-internal-ca` ClusterIssuer signs. dfe-docker Compose and a cluster with the edge module off stay on HTTP unless an issuer is named in `clickhouse.tls.issuerRef`.

| Hop | Transport |
|---|---|
| dfe-engine, its hunt runner and keda shim -> ClickHouse | HTTPS 8443, verified against the certificate's CA |
| dfe-loader -> ClickHouse | HTTPS 8443, verified |
| dfe-hyperdx -> ClickHouse | HTTPS on the connection the engine hands it, verified |
| dfe-hyperdx -> its DocumentDB store | TLS on 10260, verified against the store's cert-manager CA, SCRAM-SHA-256. The gateway serves no plaintext |
| OTel collector -> ClickHouse | Native TLS 9440, verified, under the same CA rule as the apps |
| Smoke tests and `scripts/` -> ClickHouse | HTTP 8123, plaintext |
| Apps -> in-cluster Kafka | SCRAM-SHA-512 over a plaintext listener (`kafka.securityProtocol: SASL_PLAINTEXT`) |

8123 and 9000 stay open beside 8443 and 9440 (`clickhouse.tls.required: false`), because the smoke tests dial plaintext. Keeper has no TLS. A deployment on a TLS Kafka listener sets `kafka.securityProtocol: SASL_SSL` with its `kafka.bootstrapServers` in its deploy repo's `infra/common.yaml`, and every managed broker is SASL over TLS. The DocumentDB store's PostgreSQL on 5432 serves its replicas over client-certificate TLS and its gateway over localhost. CloudNativePG's last pg_hba rule takes a SCRAM password from any address, so the chart takes the password off the superuser the operator creates with a published one (`helm/charts/documentdb/templates/documentdb.yaml`). That leaves dfe-hyperdx's login the one password role. The server keypair (`dfe-clickhouse-tls`, with `tls.key`) is readable from the clickhouse namespace alone. The apps get a copy of the CA alone, through a ClusterSecretStore that serves the namespaces it copies into and no others. Issuer choice: [deploy-tls-trust.md](deploy-tls-trust.md).

## Secrets dfe-engine mints

The engine alone reaches the engine's JWT signing key, its ClickHouse service and tenant passwords, and the OIDC client secrets an operator enters. On AWS they sit in Secrets Manager under `<path>/engine/`, encrypted with the deployment key, and the engine's Pod Identity role alone can read or write that prefix. external-secrets is denied it. Elsewhere they're files on the engine's volume. On every cloud the hunt runner and keda shim run as service accounts of their own, with no AWS identity and no Kubernetes API token, so none of the engine's grants reaches them. Grade a flaw that exposes them as an engine compromise.

dfe-receiver's ingest bearer tokens follow the same store. Off AWS the engine writes them into the `dfe-ingest-bearer-tokens` Secret as well, through a Role granting its service account `get` and `update` on that one Secret and nothing else, and the receiver mounts it read-only. A flaw that reads that Secret yields every ingest credential, so grade it as a receiver authentication bypass.

## What that means for a finding

Grade by reachability first, then severity:

1. Is the flawed code path reachable from dfe-receiver, dfe-ui, dfe-hyperdx or, where deployed, culvert? If yes, treat it seriously whatever the score, because the input is untrusted.
2. Is it reachable from outbound traffic we initiate alone, or from an operator-authenticated path? Then a mid-range score is usually a risk-accept, not a scramble.
3. Is it in a dependency of a component with no listener at all? Reaching it requires already being inside the deployment, which is a different incident.

## Recording the decision

Every finding ends in one of three states: fixed, closed as not required, or marked in the code as not-relevant or risk-accepted. Marked means AT THE LINE it concerns, with the reachability reasoning, so the next reader and the next scanner run both find it.

Where an advisory has no fixed release, marking it is the ONE resolution available short of replacing the dependency. Make the call and write it down, rather than leaving the warning to fire forever.

## Accepted risks

| Advisory | Package | Component | Decision |
|---|---|---|---|
| GHSA-qxc2-j82w-r537 | @faker-js/faker 5.5.3 | docs-site build (through docusaurus-plugin-openapi-docs, postman-collection 5.3.1) | **Not reachable.** postman-collection fills its dynamic variables from faker while CI generates the API reference pages, from the engine's own OpenAPI spec. No user input reaches it and nothing ships in the deployed stack. The fix is faker 10.x, and postman-collection 5.3.1 calls faker 5's API, so it stays until postman-collection moves. |
| RUSTSEC-2024-0449 | unsafe-libyaml 0.2.11 | dfe-transform-vrl (through vrl 0.35's serde_yaml_ng) | **Accepted.** An unmaintained notice, not a vulnerability, with no safe upgrade. 0.2.11 has no open memory-safety advisory: the one unsoundness advisory, RUSTSEC-2023-0075 (an unaligned write on 32-bit targets), was fixed in 0.2.10. No other Rust app's lock carries it. dfe-transform-vrl registers `vrl::stdlib::all()` (`src/engine/compiler.rs`), which includes VRL's `parse_yaml`, a `serde_yaml_ng::from_slice` call, so a transform program that calls `parse_yaml` hands libyaml whatever the event carries. Only a transform author writes that call. Ingested data cannot add one. Scanners are suppressed in dfe-transform-vrl's `deny.toml` and at the osv-scanner ignore in its `.hyperi-ci.yaml`. Revisit when vrl moves off serde_yaml_ng, or if a config path starts to accept YAML from an untrusted caller. |
| Design: VRL functions a transform author can call | vrl 0.35, built with default-features off (`compiler`, `stdlib-base`, `enable_crypto_functions`, `enable_system_functions`) | dfe-transform-vrl | **Accepted, narrowed.** Whoever can write a transform can run any VRL function the build includes, against every event it reads. The env and network functions (`get_env_var`, `http_request`, `dns_lookup`, `reverse_dns`) are not compiled in, so a transform cannot read the pod's environment or reach the network. The compiler refuses the functions that read a file (`parse_proto`, `encode_proto`, `validate_json_schema`, `parse_etld`'s `psl` argument) and the decoders with no output limit (`decode_gzip`, `decode_zlib`, `decode_zstd`, `decode_lz4`, `decode_snappy`), as undefined functions (`src/engine/compiler.rs`, `REFUSED_FUNCTIONS`). Only a user the engine authenticates and authorises for transform writes can author a program. Ingested data cannot. Revisit when a vrl upgrade adds a stdlib function that reads the host, or a bounded decoder lands. |
| Design: fetcher credential references | `fetcher.config` credential fields (`vault:`, `bao:`, `openbao:`, `env:`, `file:`) | dfe-fetcher, dfe-engine | **Accepted.** The engine checks only that a fetcher credential is a reference, and the fetcher resolves it with the pod's own privileges (`crates/fetcher/src/config/builtin.rs`, `credential()`). The source author also sets the host the credential is sent to. So whoever holds `source:write` (`data_analyst`, `ingest_admin`) can reach any credential the fetcher pod can: the Kafka SASL credential in its environment, and every other source's credential. Where several orgs share one fetcher, that reach crosses the org fence. Only a user the engine authenticates and authorises can write a source, and every source is committed to the deploy repo's git history. Ingested data cannot set one. Grant `source:write` only to people trusted with the fetcher's credentials. Revisit if `source:write` goes to a role that should not hold platform credentials, or if one fetcher serves orgs that do not trust each other. |
| Design: no rate limit inside the dfe-hyperdx API | `packages/api/src/api-app.ts`, `routers/api/*`, `routers/external-api/*`, `mcp/*`, `dfe/routers/*` (CodeQL js/missing-rate-limiting) | dfe-hyperdx | **Accepted.** Every route checks the engine's token before it does any work. The gateway rate-limits dfe-hyperdx's public route whenever `ui.public.hyperdx` publishes it (`ui.rate_limit`, on by default: a 6,000 a minute bucket the route's callers share and 1,200 a minute per client address, both per proxy replica). A limiter inside the fork would edit upstream Express files that every upstream merge has to carry. Not covered: the internal listener (private hostname, over the VPN) and dfe-docker, whose oauth2-proxy has no limiter. Both reach dfe-hyperdx only after login. Revisit if dfe-hyperdx takes a credential itself or stops checking the engine token per request. |
| zod v3 array walk | zod 3.25.76 | dfe-hyperdx | **Mitigated, not fixed.** Any signed-in member reaches the v3 array walk through `/dashboards`, `/saved-search` and `/pinned-filters` request bodies, and the MCP SDK runs zod's v4 core on the engine-only `/mcp`. dfe-hyperdx's `security/patches/0004` answers 413 to a JSON body past 2,000 array elements or 20,000 object members before any route validates it. No 3.x release carries the fix. The 4.x copies in the repository are eslint, knip and Stryker tooling, not in the image. Scanners are suppressed with this reason in dfe-hyperdx's `osv-scanner.toml`. Revisit when a 3.x fix ships or the fork's routes move to zod 4. |
| Design: webhook URL guard | `packages/api/src/utils/validators.ts` | dfe-hyperdx | **Not reachable in DFE.** The guard fails open on a host that does not parse and checks no resolved address, so a name resolving to a cluster address passes. In DFE mode webhooks, alerts, MCP and the external API are engine-only (`requireServicePrincipal`, `api-app.ts`), and the engine creates no webhooks. Revisit if webhooks open to users, or with DFE auth off. |
| Design: operator Keeper policy matches its label in any namespace | clickhouse-operator 0.0.8, its managed Keeper NetworkPolicy (`internal/controller/networkpolicy.go:17-24`) | ClickHouse Keeper, cluster mode | **Accepted.** The policy the `KeeperCluster` turns on admits any pod labelled `clickhouse.com/role: operator`, from any namespace, to 2181, 2281 and 9123. So whoever can create a pod in any namespace can send Keeper four-letter words and create znodes at its root. The `KeeperCluster` holds the words to read-only ones (`clickhouse.keeper.fourLetterWords`), so `rcvr`, `rqld`, `ydld` and `csnp` are switched off, and znode creation at the root stays a denial-of-service route. They cannot read or write the znodes the servers create, which carry the servers' digest ACL. `data-ingress-policy` leaves the keepers out, so pods in the namespaces it lists need an operator or server role label, and raft 9234 takes keepers alone. A labelled pod in a namespace that policy never listed reaches the client ports as well. Policies are additive, so dfe-infra cannot narrow the operator's policy. Revisit when the operator scopes that peer to its own namespace, or if a deployment grants pod creation to anyone it does not trust with its data layer. |
