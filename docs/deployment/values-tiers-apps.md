# Tier and app values

The tier profiles and per-app files in `argocd/values/` carry a pointer here and little else. This page is the reasoning behind their keys. Shared facts are in [values-shared.md](values-shared.md).

## Tier profiles

`profile-<tier>.yaml` merges after `common.yaml` and the cloud file, and Helm ignores keys a chart does not know. `clickhouse.mode`, `kafka.mode` and the storage models are tier defaults. The deploy repo's `infra/` overlay layers after them, so a deployer reaches `external`, `cached-object` or `tiered-block` there, and moves the matching host or bootstrap address in the same overlay. The `kafbat.enabled` toggle is scoped under `kafbat:`, because a bare top-level `enabled` would apply to every chart in the cascade.

- Slim is the smallest k8s tier, and below it is dfe-docker compose. It runs gRPC with no Kafka and no web infra UIs beyond Argo CD (headless) and Forgejo as the deploy-repo fallback. Requests are the tier's cost and limits are a runaway cap. The memory limit exists for dfe-hyperdx, a multi-process Node app that OOMs at 384Mi.
- Slim turns pressure off. With no broker the composite's `kafka_lag` term has nothing to measure, so the CPU scaler is the signal. Per-container kubelet stats are the cardinality cost the tier avoids.
- Slim and mesh set `routes.kafbat.enabled: false`. The Kafbat HTTPRoute lives in the `kafka` namespace, which they never create, and left on `envoy-gateway-config` stays OutOfSync on `namespaces "kafka" not found`.
- Single renders a plain broker StatefulSet (`templates/kafka-single.yaml`) and mirrors ClickHouse single, so it needs no Kafka operator. The Strimzi path and its operator exist at scale (`layer-scale.yaml`).
- Pressure stays on wherever a broker exists, because `kafka_lag` measures the backlog the CPU trigger misses on an I/O-bound loader. A group assigns one consumer per partition, so single's KEDA ceiling matches `defaultTopic.partitions`, and `kafka.sizing` is pinned to it. Scale's defaults would give a one-broker deployment partitions no consumer can reach.

## Scale and mesh profiles

- Scale's KEDA ceiling is a divisor of the partition count. Ten replicas over twelve partitions leave two consumers with two partitions each, and the busy pair sets the group's lag while the rest idle.
- Scale and mesh override `clickhouse.host`, because the single-mode Service does not exist in cluster mode. The operator emits a headless Service and no other, `<clickhouse.name>-clickhouse-headless`. Round-robin across it is correct because the engine creates ReplicatedMergeTree tables `ON CLUSTER`, so every node carries the table. It is not safe with unreplicated tables, which silently split rows across replicas.
- Scale overrides `kafka.bootstrapServers` because Strimzi names its bootstrap `<cluster>-kafka-bootstrap`. Partitions come from the kafka chart (`kafka.sizing`: broker count, KEDA ceiling, per-partition throughput) and feed both `num.partitions` and the landing topic, so no literal drifts from the broker's. The engine creates each source's `_land` and `_load` topics at `topicReplicationFactor`, so losing a broker loses no records.
- Kubelet series grow with the node count, so measure before widening `metricGroups`.
- Mesh is HA on the gRPC transport with no Kafka. Replicas and KEDA floors are scale's and the memory is not. On scale the broker holds records between stages. On mesh every stage holds what the next has not taken, and the receiver spends most of its memory on its buffer. Pressure stays on because the buffer-depth and memory terms carry the backlog that `kafka_lag` carries elsewhere. Replicas share load through the Service, so no partition count bounds the ceiling.

## Receiver buffer

The receiver's buffer is read with acknowledgements off. With them on, the default, the receiver builds no buffer or spool. It holds a sender's request until the loader confirms delivery, and once that hold expires the sender gets a 503 and retries from its copy, however large the figures.

With acknowledgements off, as in `apps/dfe-receiver/profile-mesh.yaml`, the buffer is the slack in the whole chain. There is no broker, so what the receiver holds is the one thing between a loader outage and the senders. An outage shorter than the buffer is invisible to them, and a longer one back-pressures them with 503 until the loader returns. A record held here has had its 2xx, so it dies with the pod.

The memory figure is stated and not left to the app's auto-detect, which takes 85% of the limit and leaves too little for the request path and the spool writer. Back-pressure starts at 80% of it. Node-local spool per replica sits on the contract's `spool` writable path, which mounts while `config.buffer.spillover.enabled` is true. The ride-out time is buffer plus spool, divided by each replica's ingest rate.

## Per-app notes

- `apps/_common.yaml` is the wiring every thin chart takes, layered after the cloud and profile files and before the component's directory. A key one component sets differently goes in that component's directory. The keys are the ones the scalo-service library reads (its README, "Values").
- dfe-ui and HyperDX export OTLP over HTTP, so they take the collector's HTTP port (4318) and not 4317.
- dfe-loader uses `protocol: http`, because the pinned ClickHouse client has no TCP row fetch and schema queries stall on 9000. The key is `protocol`, and `transport` is ignored.
- dfe-fetcher runs one pod per fetcher config, since N replicas of one config poll the same source N times. Its cursor claim is off by default, because a lost cursor re-fetches the default window and slim is not guaranteed a StorageClass. Argo never prunes the claim, so a render that renames or drops it leaves the claim and the Application OutOfSync until the claim is deleted by hand.
- dfe-archiver writes a `file://` destination to `archive.localPath` on a read-only root filesystem, so a writable directory mounts there on every cloud. `archive` is empty in the file because the cloud file or the overlay sets `localPath`, and a default here would beat it.
- All DFE consumers use the `classic` consumer-group protocol, because a broker rejects a KIP-848 member into a classic group.
- dfe-hyperdx's `wait-for-clickhouse-ca` init container holds the pod until the CA file exists, because Node reads `NODE_EXTRA_CA_CERTS` once as each process starts and the CA lands through two asynchronous copies. Its `mongodb` block is the bundled DocumentDB gateway, which serves TLS and authenticates with SCRAM-SHA-256. An external endpoint sets `uri` with credentials inline, `extraEnv.MONGO_PASSWORD` to an empty string and `caSecretName` to a Secret holding its CA under `ca.crt`.

## Fragments and culvert

The `wait-for-engine` init container holds an app until dfe-engine reports ready, when every ClickHouse object and bootstrap topic exists. It is bounded, and on expiry the pod waits in `Init:CrashLoopBackOff`. The `pressure-trigger` fragment reads the ScalingPressure gauge through the fail-safe dfe-keda-shim, which holds the last good reading through a metric outage so the outage cannot scale replicas up. KEDA takes the higher of it and the CPU trigger, and `Value` not `AverageValue` applies because the gauge is already per workload. `scripts/composition.py` copies both fragments into each app file's marked block.

culvert mounts its profile file alone (`configMount.subPath`) so the profiles the image ships stay beside it. The PKI claim is kept on prune and delete, because the CA, CRL and WireGuard peer allocations live there, and turning the edge module off keeps every issued client config. `net.ipv4.ip_forward` is set in an init container because `/proc/sys` is read-only in the server container whatever capabilities it holds. scalo-service's `publicService` cannot pin a `nodePort`, which `terraform/modules/edge/aws` forwards to, so culvert renders public Services, one per protocol family.
