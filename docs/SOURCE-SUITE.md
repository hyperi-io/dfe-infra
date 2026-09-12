# The post-deploy source tests (`acceptance --suite source`)

Not part of the stock POST: the default E2E tests prove the deploy moved data,
these prove an operator can add a source and watch it work. Run on demand after
a deploy, here and on docker through dfe-docker's `make test-source`, which
calls the same runner. The cycle these hang off is
[TESTING-CYCLE.md](TESTING-CYCLE.md).

## What runs it

`scripts/acceptance/source/` -- `run.py` is the order the steps happen in,
`steps.py` what every source shares, `cases.py` the two kinds, `fetcher.py` the
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
the Transform tab that unlocks (Define Transform -> `dfe-transform-vrl`). After
the deploy the source's Processing tab puts the bundled
`pipelines/filebeat/filebeat.vrl` and `timezones.csv` from the
dfe-transform-vrl checkout (`--transform-repo`) into the instance's file sets.
The filebeat corpus is then posted at the receiver wrapped as
`{message, tags, _source}`.

Proof: the transform instance reports, the receiver routes, new rows in
`<name>` carry `log_file_path` (only the program sets it), a zstd file appears
under `<name>_land` in the archiver, and the engine lists a HyperDX source for
it.

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
