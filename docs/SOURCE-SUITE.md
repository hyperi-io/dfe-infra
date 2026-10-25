# The post-deploy source tests (`acceptance --suite source`)

Not part of the stock POST: the default E2E tests prove the deploy moved data,
these prove an operator can add a source and watch it work. Run on demand after
a deploy, here and on docker through dfe-docker's `make test-source`, which
calls the same runner. The cycle these hang off is
[TESTING-CYCLE.md](TESTING-CYCLE.md).

## What runs it

`scripts/acceptance/source/` -- `run.py` is the order the steps happen in,
`steps.py` what every source shares, `cases.py` the kinds, `fetcher.py` the
AWS upstreams -- over `acceptance/clients.py`, the engine and datastore both
suites use. dfe-ops supplies the port-forwards, the `DFE_E2E_*` env, the
archiver exec prefix (`--archive-selector` names the pods, one exec per replica
since each archives only its own partitions) and the restart prefix.

Every step lands a screenshot under `--shots-dir`, and the run removes the
source it made (`--keep` leaves it). A step the console cannot do goes through
the API and is recorded as `api-fallback`, so the report says which half of the
product was driven.

## `filebeat` (the default case)

The console creates the source -- Configuration tab (name, display name, description, Archive on, match `data_stream.dataset equals cisco_ios.log`, Elastic's own identifier for the source), Table Settings tab (the
shipped `common-header/timeseries` 1.0.1 and `meta/beats/filebeat` 1.1.0), then
the Transform tab (Define Transform -> `dfe-transform-vrl`). After
the deploy the source's Processing tab puts the bundled
`pipelines/filebeat/filebeat.vrl` and `timezones.csv` from the
dfe-transform-vrl checkout (`--transform-repo`) into the instance's file sets.
The corpus's cisco_ios lines are then posted at the receiver in the envelope an Elastic Agent sends: `agent`, `data_stream`, `input`, the vendor line as `message`, and `tags`. Every record also carries a root `_e2e` object, `{run: <run id>, kind: corpus|probe}`, and the counts read that rather than `tags`.

One `equals` match routes one dataset, so the cisco_meraki and cisco_umbrella lines are not fed and this suite does not cover them.

Archive stays off where the engine lists no `dfe-archiver` instance: on direct the receiver also sends an archived record to the archiver, and with none it answers 503.

Proof: the transform instance reports, the receiver routes, every corpus record posted lands in `<name>` with none in `main` (`landed`, counted by `_e2e`, since the routing probes land in the same table), every row carries `data_stream.dataset` `cisco_ios.log` (`dataset`), the run's corpus rows carry `source_ip`, a zstd file appears under `<name>_land` in the archiver, and the engine lists a HyperDX source for it.

`reporting` is not taken at face value. The engine answers it off the otel
tables, and on Compose one container serves the app and every instance of it, so
the step looks for the instance's own series and for scalo's `pipeline_idle` --
which an app publishes only while it holds no work -- and records `unproven`
where an idle app's telemetry cannot be told from a working instance's
(dfe-infra #327). An `unproven` row is neither a pass nor a failure and is
repeated under the table, so a long report cannot be read as green.

Last, for every case, the `observe` step opens the console's Observe search,
picks the source in the embedded HyperDX and reads a non-zero results line. The
console iframes HyperDX from a second origin, so this is the step that meets
what a tester meets: the embed's frame-ancestors, the login shared across the
two origins and the source's own view. A frame the browser refused fails the
step with the console's own error line.

## The record ceiling (`record-ceiling`, every case)

After a case's own proof, the step probes the receiver's door, the request body limit it runs with. One record 1 KiB over the door must be answered 413 and never land. One 1 KiB under must be accepted and land in `main`, which proves every hop behind the door, the broker topic included, takes what the door admits.

dfe-ops reads the door off the dfe-receiver Deployment's `DFE_RECEIVER_MAX_BODY_SIZE`, which a managed broker sets to 5/8 of its `kafka.message_max_bytes`, else the receiver's own 10 MiB. A run with no `--receiver-door-bytes` records the step `skipped`.

## `elastic`

The same push and the same meta schema as `filebeat`, against
dfe-transform-elastic instead. Three things differ.

The app carries one compiled-in transform per Elastic data stream and an instance runs one of them, so the source's `transform` block needs a `variant` as well as an `engine`: `filebeat.cisco_ios.default`, as the dfe-schemas catalogue lists it, which the deploy writes to the instance's own `config.source.name`. The console's Transform tab has no control for it, so `attach-transform` is an `api-fallback` whose detail names how far the console got. Without the variant the engine writes nothing to that key and the instance starts on no transform at all.

And there is no `upload-program`: the app declares no file sets, so the step
records `skipped` and the run writes nothing to the instance.

The source's brokers are the deployment's, not the source's. The bus profiles hand the app `kafka.bootstrapServers` as `DFE_TRANSFORM_ELASTIC_SOURCE_BROKERS`, which it applies over its config file, and `apps.yaml` declares `config.source.brokers` deployment-owned, so the engine refuses a write to it and a `brokers` list in the instance overlay never reaches the consumer.

The cisco_ios lines the filebeat case posts are also the entry the variant names, and the proof is the same `source_ip` column, which the posted envelope carries nothing of.

## Pushing through a real filebeat (`--via logstash`, `--via beats`)

The posted envelope is reconstructed. A deployment gets the agent's own, two ways: filebeat shipping lumberjack to Logstash and Logstash's http output posting the event at the receiver (`--via logstash`), or filebeat shipping lumberjack straight to the receiver's own listener (`--via beats`). The Elastic ingest pipelines dfe-transform-elastic compiles in were written against that envelope. Both are variations of the two pushed cases.

Both stand containers on the stack's docker network (`--beats-network`), write this run's corpus slice to a file filebeat tails, and wait for routing over HTTP before it ships. The shippers are 9.5.5, the release a site runs now. They do not track the 9.2.2 Elasticsearch release dfe-transform-elastic compiles its pipelines from. The filebeat config is the operator's: it stamps the `data_stream` the source is matched on with `add_fields`, as an integration policy does, with namespace `acceptance`, plus the `_e2e` corpus marker and an `e2e_run` tag. Teardown removes every container the run started (`beats-removed`).

`--via logstash` adds Logstash, which adds nothing and posts each pipeline batch as one JSON array (`format => "json_batch"`) at `--beats-receiver-url`. The receiver splits the array into one record per element.

`--via beats` ships to `--beats-lumberjack` (`dfe-receiver:5044`). The listener ships off, and an enabled one refuses to start without client certificates unless it accepts every client that can reach the port. So the run sets `config.lumberjack.enabled` and `config.lumberjack.accept_unauthenticated` through the engine's app config route (`receiver-lumberjack`), applies the restart that reports with `--restart-exec` (`receiver-restart`), and puts back whatever the overlay held there at teardown (`receiver-lumberjack-restored`). dfe-docker publishes no beats port by default, so only the stack's network reaches it. An engine that refuses the write fails the run and names the fallback: `DFE_RECEIVER_LUMBERJACK__ENABLED=true` and `DFE_RECEIVER_LUMBERJACK__ACCEPT_UNAUTHENTICATED=true` in the receiver's environment.

`logstash` and `filebeat` say the containers came up, and `feed` carries filebeat's own acked count, so an agent that shipped nothing reads differently from a stack that took nothing. Two rows read the stored rows back. `envelope` names the fields one arrived with, plus `log.file.path`, which only a Beats agent sets. `envelope-kept` holds every row off the agent to `agent.type` `filebeat`, which filebeat sets, and `data_stream.namespace` `acceptance`, which the operator stamped. Elasticsearch stores both as they arrived, and the beats meta schema makes a column of `agent.type`.

Compose only. The Kubernetes tier wants a Job instead, which is not built.

## `cloudwatch`, run as `--aws-service cloudtrail`

The meta schema is authored by hand in the console, the fetcher stanza goes
through the API (no fetcher origin in the console yet, dfe-ui #286), and the
proof is rows arriving on their own within a poll interval. A sixth env file
names the upstream (`.tmp/aws-test.env`: `DFE_AWS_REGION`,
`DFE_AWS_LOG_GROUP`), and `--fetcher-credentials-vault-path kv/<path>` reads
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` out of the secrets store into
the Secret the fetcher reads (`argocd/values/apps/dfe-fetcher` `credentials.secretName`)
before the source is created, so no run mints it by hand; nothing is written to
AWS. The test account's CloudWatch Logs group is empty by construction, so
CloudTrail is the service that proves anything.

## Restarting an app the writes cannot reach

The create reply, the deploy reply and each file-set write carry
`restart_required`: one command per app whose running process cannot take that
change where it stands. The engine works it out, so the runner does not. It is
only ever non-empty where the engine renders the app's config itself -- a
Compose deployment that named a config directory -- and only where the write
changed something the process cannot pick up; a Kubernetes deploy reports none,
because the chart's checksum rolls the pod.

On the direct transport the create is the write that adds the source's
destination to the receiver, and the receiver builds its senders once at
startup, so only the create's reply names it. The console makes that create and
shows no hint, so the runner reads the reply the console was handed.

`--restart-exec` is the command prefix that applies one; the runner appends the
service the hint's `make apply SERVICES=<service>` names. On Compose the prefix
must run that same `make apply` in the deployment's checkout, as dfe-docker's
does: a source's own transform is a container the engine declares on the
deploy, and only `make apply` creates it, where `docker compose restart` answers
"no such service". A run that is handed a hint and no prefix fails the step
rather than passing quietly, because without the restart the app takes the new
file and goes on consuming the topics it started with, and the source never
moves a record.
