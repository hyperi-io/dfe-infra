# The post-deploy source tests (`acceptance --suite source`)

These aren't part of the stock POST. The default E2E tests prove the deploy moved data. These prove an operator can add a source and watch it work. Run them on demand after a deploy, here and on docker through dfe-docker's `make test-source`, which calls the same runner. The cycle they hang off is [testing-cycle.md](testing-cycle.md).

## What runs it

The code is in `scripts/acceptance/source/`. `run.py` is the order the steps happen in, `steps.py` what every source shares, `cases.py` the kinds, and `fetcher.py` the AWS upstreams. They sit over `acceptance/clients.py`, the engine and datastore both suites use. dfe-ops supplies the port-forwards, the `DFE_E2E_*` env, the archiver exec prefix (`--archive-selector` names the pods, one exec per replica since each archives only its own partitions) and the restart prefix.

Every step lands a screenshot under `--shots-dir`, and the run removes the source it made (`--keep` leaves it). A step the console can't do goes through the API and is recorded as `api-fallback`, so the report says which half of the product was driven.

## `filebeat` (the default case)

The console creates the source. In the Configuration tab that's the name, display name, description, Archive on, and the match `data_stream.dataset equals cisco_ios.log` (Elastic's own identifier for the source). In the Table Settings tab it's the shipped `common-header/timeseries` and `meta/beats/filebeat`, at the `HEADER_VERSION` and `META_SCHEMA_VERSION` in `scripts/acceptance/source/cases.py`. Then the Transform tab (Define Transform -> `dfe-transform-vrl`). After the deploy, the source's Processing tab puts the bundled `pipelines/filebeat/filebeat.vrl` and `timezones.csv` from the dfe-transform-vrl checkout (`--transform-repo`) into the instance's file sets.

The cisco_ios lines of dfe-engine's synthetic corpus (`tests/e2e/filebeat_cases.yaml`) are then posted at the receiver in the envelope an Elastic Agent sends: `agent`, `data_stream`, `input`, the device line as `message`, and `tags`. Every record has a root `_e2e` object as well, `{run: <run id>, kind: corpus|probe}`, and the counts read that rather than `tags`.

One `equals` match routes one dataset, so the cisco_meraki and cisco_umbrella lines aren't fed and this suite doesn't cover them.

Archive stays off where the engine lists no `dfe-archiver` instance. On direct the receiver also sends an archived record to the archiver, and with none it answers 503.

The proof:

- The transform instance reports and the receiver routes.
- Every corpus record posted lands in `<name>` with none in `main` (`landed`, counted by `_e2e`, since the routing probes land in the same table).
- Every row carries `data_stream.dataset` `cisco_ios.log` (`dataset`).
- The run's corpus rows carry `source_ip`.
- A zstd file appears under `<name>_land` in the archiver.
- The engine lists a dfe-hyperdx source for it.

`reporting` isn't taken at face value. The engine answers it off the otel tables, and on Compose one container serves the app and every instance of it. So the step looks for the instance's series and for scalo's `pipeline_idle` (which an app publishes when it holds no work, and not otherwise), and records `unproven` where an idle app's telemetry can't be told from a working instance's (dfe-infra #327). An `unproven` row is neither a pass nor a failure and is repeated under the table, so a long report can't be read as a pass.

Last, for every case, the `observe` step opens the console's Observe search, picks the source in the embedded dfe-hyperdx and reads a non-zero results line. The console iframes dfe-hyperdx from a second origin, so this step meets what a tester meets: the embed's frame-ancestors, the login shared across the two origins and the source's view. A frame the browser refused fails the step with the console's error line.

## The record ceiling (`record-ceiling`, every case)

After a case's proof, the step probes the receiver's request body limit. One record 1 KiB over the limit must be answered 413 and never land. One 1 KiB under must be accepted and land in `main`, which proves every hop behind the receiver, the broker topic included, takes what the receiver admits.

The limit is the DFE-wide record cap, 8,000,000 bytes, which the dfe-extras chart sets on every receiver whatever the broker. So the step sends a 7,998,976-byte record that must land and an 8,001,024-byte record that must be refused. `--receiver-door-bytes` names another limit, and `--receiver-door-bytes 0` records the step `skipped`.

## `elastic`

The same push and the same meta schema as `filebeat`, against dfe-transform-elastic instead. Three things differ.

The app carries one compiled-in transform per Elastic data stream and an instance runs one of them. So the source's `transform` block needs a `variant` as well as an `engine`: `filebeat.cisco_ios.default`, as the dfe-schemas catalogue lists it, which the deploy writes to the instance's `config.source.name`. The console's Transform tab has no control for it, so `attach-transform` is an `api-fallback` whose detail names how far the console got. Without the variant the engine writes nothing to that key and the instance starts on no transform at all.

There's no `upload-program`. The app declares no file sets, so the step records `skipped` and the run writes nothing to the instance.

The source's brokers are the deployment's, not the source's. The bus profiles hand the app `kafka.bootstrapServers` as `DFE_TRANSFORM_ELASTIC_SOURCE_BROKERS`, which it applies over its config file, and `apps.yaml` declares `config.source.brokers` deployment-owned. So the engine rejects a write to it, and a `brokers` list in the instance overlay never reaches the consumer.

The cisco_ios lines the filebeat case posts are the entry the variant names as well, and the proof is the same `source_ip` column, which the posted envelope doesn't carry.

## `vector`

This is the filebeat case with the program run by dfe-transform-vector, a supervised Vector. Its bundled `pipelines/filebeat/filebeat.yaml` embeds dfe-transform-vrl's `filebeat.vrl` byte-identical in a Vector `remap`, with the same `timezones.csv`. So the proof is the same `source_ip` column from a second runtime.

The program comes from dfe-transform-vector, through `--program-repo`, which defaults to the checkout beside the engine repo. dfe-ops reads the second from `DFE_TRANSFORM_VECTOR_REPO`, as dfe-docker's `make test-source` does.

The docker single seeds one idle dfe-transform-vector, so Compose can reach the case. dfe-docker's `make proof` runs it after `filebeat` on every stack version, with dfe-transform-vector cloned at the tag the stack pins.

## Pushing through a filebeat agent (`--via logstash`, `--via beats`)

The posted envelope is reconstructed. A deployment gets the agent's, two ways. Filebeat ships lumberjack to Logstash and Logstash's http output posts the event at the receiver (`--via logstash`), or filebeat ships lumberjack straight to the receiver's listener (`--via beats`). The Elastic ingest pipelines dfe-transform-elastic compiles in were written against that envelope. Both are variations of the two pushed cases.

Both stand containers on the stack's docker network (`--beats-network`), write this run's corpus slice to a file filebeat tails, and wait for routing over HTTP before it ships. The shippers run at `SHIPPER_VERSION` in `scripts/acceptance/source/beats.py`, newer than the Elastic release dfe-transform-elastic compiles its pipelines from (its `docs/compat.md`). The filebeat config is the operator's. It stamps the `data_stream` the source is matched on with `add_fields`, as an integration policy does, with namespace `acceptance`, plus the `_e2e` corpus marker and an `e2e_run` tag. Teardown removes every container the run started (`beats-removed`).

`--via logstash` adds Logstash, which adds nothing and posts each pipeline batch as one JSON array (`format => "json_batch"`) at `--beats-receiver-url`. The receiver splits the array into one record per element.

`--via beats` ships to `--beats-lumberjack` (`dfe-receiver:5044`). The listener ships off, and an enabled one refuses to start without client certificates unless it accepts every client that can reach the port. So the run sets `config.lumberjack.enabled` and `config.lumberjack.accept_unauthenticated` through the engine's app config route (`receiver-lumberjack`), applies the restart that reports with `--restart-exec` (`receiver-restart`), and puts back whatever the overlay held there at teardown (`receiver-lumberjack-restored`). dfe-docker publishes no beats port by default, so the stack's network alone reaches it. An engine that refuses the write fails the run and names the fallback: `DFE_RECEIVER_LUMBERJACK__ENABLED=true` and `DFE_RECEIVER_LUMBERJACK__ACCEPT_UNAUTHENTICATED=true` in the receiver's environment.

`logstash` and `filebeat` say the containers came up, and `feed` has filebeat's acked count, so an agent that shipped nothing reads differently from a stack that took nothing. Two rows read the stored rows back:

- `envelope` names the fields one arrived with, plus `log.file.path`, which a Beats agent alone sets.
- `envelope-kept` holds every row off the agent to `agent.type` `filebeat`, which filebeat sets, and `data_stream.namespace` `acceptance`, which the operator stamped. Elastic stores both as they arrived, and the beats meta schema makes a column of `agent.type`.

Compose alone. The Kubernetes tier wants a Job instead, which isn't built.

## `syslog`

The other cases reach the receiver over HTTP or lumberjack, so a drop in its syslog listeners never shows in them. This one sends the dfe-docker demo's cisco-asa and fortinet lines (the first 20 of each, copied in under `scripts/acceptance/source/fixtures/syslog/` from dfe-docker `9e06976b`) as bare RFC 3164 syslog, with the run id as the header's hostname.

The case switches the syslog listeners on through the engine API (`receiver-syslog`) and puts them back at teardown, like the lumberjack listener. An unauthenticated listener is never a default (`scripts/tests/test_default_config_no_test_keys.py`). They bind the contract's 514 TCP and UDP, and the receiver overlay (`argocd/values/apps/dfe-receiver/values.yaml`) sets a pod sysctl so uid 1000 can bind 514. The case then creates a source per device type named for the label the detector stamps (`cisco-asa`, `fortinet`), deployed and then set dormant, and a run-named agentless dfe-transform-elastic intake on `_source equals syslog` that detects the device type per line. The intake's topics are fresh every run. A device-type source the deployment holds is used as it is and never removed. Counts are the table's gain, so rows there before do not count.

## `syslog`: transports and counts

Each transport sends the whole corpus: TCP newline framed, TCP octet counted (RFC 6587) and UDP. On TCP a second connection sends a good line, one past `syslog.max_message_size` (64 KiB) and a good line. From receiver v1.15.64 the oversize message is discarded and counted, and the connection goes on, so the good line after it must land. Per transport (`syslog-tcp-lf`, `syslog-tcp-octet`, `syslog-udp`) each device-type table must gain exactly what was sent to it. The detail ends on the hop at fault: lines sent, lines the receiver accepted, rows the device-type tables got, rows in the intake, run-tagged rows that reached `main`. Then it counts the transport's run tag (the syslog header's hostname) in each device-type table, the intake and `main`, and fails unless every tagged row sits in the table its device type maps to, so a row in the wrong table is named. Every count is read on each replica through `clusterAllReplicas` and the fullest one counts, because a part reaches the other replicas after the one that took the INSERT. The detail names the replica that held a device type's rows first and when every replica had them, so a slow fetch shows without failing the step. A datastore with no cluster is counted directly. A transport waits up to 180 s, and stops at 30 s when no new row reached any replica. A receiver that accepted every line while the tables stayed short reads `Rows stopped at the loader`, which is the loader's consumer not picking up the run's fresh topics after an earlier source's topics were deleted. `syslog-counters` then holds `receiver_requests_total` to the good frames sent, `receiver_records_dropped_total{reason="oversize"}` to the oversize frames sent, any other drop reason to zero, and `receiver_parse_failures_total` to zero (label `transport="syslog"` covers all three listeners). The counters are summed over every running pod `--receiver-metrics-selector` matches, because kube-proxy gives each connection to one replica. Docker reads `--receiver-metrics-url`, and with neither the step reads `unproven`.

`syslog-tls` reads `skipped` with `NOT COVERED`: the receiver chart has no TLS syslog listener, and no overlay sets `syslog.tls.enabled`.

dfe-ops sends from inside the cluster, because `kubectl port-forward` carries TCP only: `--syslog-exec` is `kubectl exec -i` into the first archiver pod, and bash there opens `/dev/tcp` and `/dev/udp` to `--syslog-host` (default `dfe-receiver`). It reads the receiver's counters with `curl` inside every pod `--receiver-selector` matches. With no `--syslog-exec` the runner connects from its machine.

## `edgestream-hub`

An EdgeStream Hub's syslog feed, posted the way the hub posts it: a gzip JSON array, each event a syslog-ng RFC 5424 event inside the hub's `@edgestream` object. Nothing in it names a DFE source, so the source routes on `@edgestream.event.src_type equals syslog_ng`, a field the hub sends.

The engine API creates the source from the dfe-schemas catalogue entry `edgestream-hub-syslog`. There is no console form to drive, no transform, no archive and no app instance, so those steps are left out. A source of that name on the stack when the run starts is read as it stands and left deployed.

`fixtures/edgestream/hub-syslog.json` holds six events of the hub's shape with made-up values. Each run stamps them a second apart, adds the `_e2e` marker and posts them as two arrays. A seventh event with `src_type` `vector` goes in a third.

The proof:

- `source-match`: the entry reads back with the hub's field as its match. An engine without the matching catalogue change reads it back as `_source` and fails here.
- `routed`: a probe event reaches `dfe.edgestream-hub-syslog`.
- `landed`: every event lands in the table, with none in `main`.
- `typed-columns`: every row fills `host`, `host_from`, `facility`, `message`, `event_name`, `event_src_type`, `hub_org_id` and `hub_site_id`, the columns the hub's sample shows it sends.
- `rfc5424-columns`: every row fills `severity` and `program`, which come from the other RFC 5424 fields syslog-ng adds.
- `event-time`: each row's `_timestamp` and `timestamp` are the second its `@timestamp` names, not the load time.
- `other-src-type`: the `vector` event reaches `main` and never the table.

Run it with `--case edgestream-hub`, or `dfe-ops acceptance --suite source --source-case edgestream-hub`. dfe-docker's `make test-source` offers no such case yet.

## `flow`

NetFlow v5 and v9, IPFIX and sFlow v5 datagrams, sent at the receiver's UDP flow listener from the captures dfe-receiver commits under `tests/fixtures/flow/external`. The engine creates the catalogue sources `netflow` and `sflow`, and the run switches the listener on through the engine in `exploded` mode, one event per flow record, and puts it back after.

The run needs the receiver's UDP ports reachable from the machine it runs on. On dfe-docker that is `DFE_RECEIVER_NETFLOW_PORT` and `DFE_RECEIVER_SFLOW_PORT` in `.env`, which publish ports 2055 and 6343 (docs/configuration.md there). `--flow-host` names the address, the host of the receiver's ingest URL when omitted, and `--flow-netflow-port` and `--flow-sflow-port` name the ports. The receiver senses NetFlow, IPFIX and sFlow per datagram, so one port would take all three. The case reads the fixtures from the dfe-receiver checkout beside the engine repo, or from `--transform-repo`.

The sender is a local UDP socket. The exec-prefix sender the syslog case uses splits datagrams on a NUL byte, which a binary datagram holds, so a cluster with no UDP route from the runner is not covered.

Each fixture is sent in order, a datagram apiece. Template state is per exporter, and each file carries the templates its own data needs. The proof is rows gained, not rows held, so a table that already has flows does not fool it:

- `flow-netflow-landed`, `flow-sflow-landed`: the table gained as many rows as dfe-receiver's `flow_exploded` test says the fixtures yield, and no record of that protocol reached `main`.
- `flow-netflow-typed`, `flow-sflow-typed`: the rows carry `exporter_ip` and `flow_version`, and the flow records that have a source address carry `src_ip`.

Run it with `--case flow`, or `dfe-ops acceptance --suite source --source-case flow`. dfe-docker's `make test-source` offers no such case yet.

## `cloudwatch`, run as `--aws-service cloudtrail`

The meta schema is authored by hand in the console. The fetcher stanza goes through the API (no fetcher origin in the console yet, dfe-ui #286). The proof is rows arriving on their own within a poll interval.

`--aws-region` and `--aws-log-group` (or `DFE_E2E_AWS_REGION` and `DFE_E2E_AWS_LOG_GROUP`) name the upstream, and `--aws-service` picks CloudWatch Logs (the default) or CloudTrail. `--fetcher-credentials-vault-path kv/<path>` reads `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` out of the secrets store into the Secret the fetcher reads (`argocd/values/apps/dfe-fetcher` `credentials.secretName`) before the source is created, so no run mints it by hand. Nothing is written to AWS. The test account's CloudWatch Logs group is empty by construction, so CloudTrail is the service that shows anything.

## Restarting an app the writes cannot reach

The create reply, the deploy reply and each file-set write carry `restart_required`: one command per app whose running process can't take that change where it stands. The engine works it out, so the runner doesn't. It's non-empty where the engine renders the app's config itself, which is a Compose deployment that named a config directory, and where the write changed something the process can't pick up, and nowhere else. A Kubernetes deploy reports none, because the chart's checksum rolls the pod.

On the direct transport the create is the write that adds the source's destination to the receiver, and the receiver builds its senders once at startup, so the create's reply alone names it. The console makes that create and shows no hint, so the runner reads the reply the console was handed.

`--restart-exec` is the command prefix that applies one. The runner appends the service the hint's `make apply SERVICES=<service>` names. On Compose the prefix must run that same `make apply` in the deployment's checkout, as dfe-docker's does. A source's transform is a container the engine declares on the deploy, and `make apply` alone creates it, where `docker compose restart` answers "no such service". A run that is handed a hint and no prefix fails the step rather than passing quietly. Without the restart the app takes the new file and goes on consuming the topics it started with, and the source never moves a record.
