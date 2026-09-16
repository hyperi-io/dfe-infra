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

The console creates the source -- Configuration tab (name, display name,
description, Archive on, match `_source equals <name>`), Meta Schema tab (the
shipped `common-header/timeseries` 1.0.1 and `meta/beats/filebeat` 1.0.0), then
the Transform tab that then becomes available (Define Transform -> `dfe-transform-vrl`). After
the deploy the source's Processing tab puts the bundled
`pipelines/filebeat/filebeat.vrl` and `timezones.csv` from the
dfe-transform-vrl checkout (`--transform-repo`) into the instance's file sets.
The filebeat corpus is then posted at the receiver wrapped as
`{message, tags, _source}`.

Proof: the transform instance reports, the receiver routes, new rows in
`<name>` carry `log_file_path` (only the program sets it), a zstd file appears
under `<name>_land` in the archiver, and the engine lists a HyperDX source for
it.

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

## `elastic`

The same push and the same meta schema as `filebeat`, against
dfe-transform-elastic instead. Two things differ.

The app carries one compiled-in transform per Elastic data stream and an
instance runs one of them, so the source's `transform` block needs a `variant`
as well as an `engine`: `filebeat.cisco_ios.default`, which is
dfe-transform-elastic's `sources.yaml` entry rendered through this repo's
`apps.yaml` `catalogue.variant_pattern`, and which the deploy writes to the
instance's own `config.source.name`. The console's Transform tab has no control
for it, so `attach-transform` is an `api-fallback` whose detail names how far
the console got. Without the variant the engine writes nothing to that key and
the instance starts on no transform at all.

And there is no `upload-program`: the app declares no file sets, so the step
records `skipped` and the run writes nothing to the instance.

The corpus is narrowed to its `cisco_ios` module, which is the entry the variant
names. cisco_umbrella is delivered out of an S3 bucket and takes no receiver
intake; cisco_meraki's pipeline reads a body rather than a syslog line.

Proof: new rows in `<name>` carry `source_ip`. `meta/beats/filebeat` declares
it, the cisco_ios transform reads it out of the syslog body, and the posted
`{message, tags, _source}` record carries nothing of the sort.

## Pushing through a real filebeat and logstash (`--via logstash`)

The wrapper's `{message, tags, _source}` proves the transform and is not how a
deployment is fed. The common path is a Beats agent shipping lumberjack to
Logstash and Logstash's http output posting its whole event at the receiver, and
the Elastic ingest pipelines dfe-transform-elastic compiles in were written
against that envelope. `--via logstash` is that variation of the two pushed
cases rather than a case of its own, so the same source, the same waits and the
same proof all still apply.

It stands a filebeat and a logstash container beside a compose deployment --
`--beats-network` names the docker network the stack runs on and
`--beats-receiver-url` the ingest URL as seen from inside it -- writes this run's
corpus slice to a file the agent tails, and lets the pair carry it. A logstash
filter adds the `_source` field the source is matched on; everything else in the
event is the envelope filebeat and logstash built. The run still waits for
routing over HTTP first, so the receiver has rolled onto the new rule before the
agent ships, and its teardown removes both containers.

Three rows of its own: `logstash` and `filebeat` say the pair came up, `feed`
carries filebeat's own acked count (its count, not the receiver's, so an agent
that shipped nothing is a different finding from a stack that took nothing), and
`envelope` names the fields the record that landed arrived with, plus its
`log.file.path` -- the field only a Beats agent sets and the one the wrapper
cannot produce.

Compose only. The Kubernetes tier wants a Job instead, which is not built.

## `cloudwatch`, run as `--aws-service cloudtrail`

The meta schema is authored by hand in the console, the fetcher stanza goes
through the API (no fetcher origin in the console yet, dfe-ui #286), and the
proof is rows arriving on their own within a poll interval. A sixth env file
names the upstream (`.tmp/aws-test.env`: `DFE_AWS_REGION`,
`DFE_AWS_LOG_GROUP`), and `--fetcher-credentials-vault-path kv/<path>` reads
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` out of the secrets store into
the Secret the chart names (`helm/charts/dfe-fetcher` `credentials.secretName`)
before the source is created, so no run mints it by hand; nothing is written to
AWS. The test account's CloudWatch Logs group is empty by construction, so
CloudTrail is the service that proves anything.

## Restarting an app the writes cannot reach

The deploy reply and each file-set write carry `restart_required`: one command
per app whose running process cannot take that change where it stands. The
engine works it out, so the runner does not. It is only ever non-empty where the
engine renders the app's config itself -- a Compose deployment that named a
config directory -- and only where the write changed something the process
cannot pick up; a Kubernetes deploy reports none, because the chart's checksum
rolls the pod.

`--restart-exec` is the command prefix that applies one (dfe-docker passes
`docker restart`); the app is the last word of the hint. A run that is handed a
hint and no prefix fails the step rather than passing quietly, because without
the restart the app takes the new file and goes on consuming the topics it
started with, and the source never moves a record.
