---
sidebar_position: 1.6
sidebar_label: Coming from a lakehouse
slug: /coming-from-lakehouse
---

# Coming from a lakehouse

If you've run Snowflake, Databricks, BigQuery, or Athena over Iceberg or Delta tables, you'll bring a warehouse's assumptions about how data lands and what a query costs. DFE is built for a different job: high-volume machine data, landed in seconds, kept for months and queried without a bill per scan. This page says where the two differ and where DFE sits beside a warehouse you keep.

## Schema first, or land then promote

A warehouse wants the model before the data arrives. Databricks Auto Loader stops the stream on a new column and picks the change up on restart, with mismatched values parked in a `_rescued_data` JSON column ([Auto Loader schema docs](https://docs.databricks.com/aws/en/ingestion/cloud-object-storage/auto-loader/schema), read 2026-11-13). Snowflake evolves a table's schema only on `COPY INTO` and Snowpipe loads, ten new columns per load by default, and a `VARIANT` column answers `NULL` for a path that isn't there ([Snowflake schema evolution](https://docs.snowflake.com/en/user-guide/data-load-schema-evolution), read 2026-11-13). Either way somebody owns the model, and a supplier renaming a field is a ticket.

DFE lands every field from the first event. A field with no mapping yet lands in automatic typed columns and is queryable at once, as `_json.a.b`. When a field earns a column, promote it in the meta schema (`dfe-schemas`) and new records land in it, while older ones stay readable where they were. Nothing stops, nothing restarts, and no pipeline re-runs. You spend schema effort only where it pays back.

## Pay per scan, or a column store you run

BigQuery on demand and Athena bill by the bytes a query scans, Snowflake by the seconds a virtual warehouse runs, and Databricks SQL by DBUs ([BigQuery pricing](https://cloud.google.com/bigquery/pricing), [Athena pricing](https://aws.amazon.com/athena/pricing/), [Snowflake pricing](https://www.snowflake.com/en/pricing-options/), [Databricks SQL pricing](https://www.databricks.com/product/pricing/databricks-sql), read 2026-11-13). On machine data that model points one way: the analyst learns to scan less, and the dashboard that used to refresh every minute refreshes every hour.

DFE stores each source as ClickHouse columns on hardware you size once, in your cluster or ClickHouse Cloud. A query costs CPU you already own. The sort key prunes the parts a time range touches, columns compress to a fraction of the raw bytes, and a dashboard refreshing every minute costs nothing extra. In the August 2025 tests with AWS and ClickHouse, DFE kept 270 days queryable where the index engine beside it kept 14, on 10 to 25 percent of the resources ([the test](coming-from.md#stored-as-clickhouse-columns)). That was against OpenSearch, not a warehouse, and your data's shape sets your own number.

## ELT and dbt, or promote in place

In a lakehouse, raw data lands in bronze, a dbt model or a Spark job rebuilds silver and gold on a schedule, and a change to the model means a rebuild over the history. DFE parses once, at the edge: dfe-receiver and dfe-fetcher turn the transport into JSON on the first hop, the transforms parse vendor message bodies after that, and the loader writes typed columns. Promoting a field changes the table forward. There is no silver layer to rebuild, and the question is answered from the table the data landed in.

Context joins the same way it does in a warehouse. Fact tables hold the events, logs and metrics, dimension tables hold the inventory, the identity directory and the CMDB, and you join them in ClickHouse SQL ([sources join](coming-from.md#sources-join)).

## Minutes, or seconds

Snowpipe loads staged files and Auto Loader processes micro-batches, so an event is queryable some minutes after it happened. DFE is a streaming path: receiver, Kafka or direct gRPC, loader, table. The smoke test every deploy runs posts one record and reads it back from the table before anyone touches the stack. A rule on a source runs on a schedule you choose, and a hunt looks back over months of the same table ([detections and hunts](operate/detections-and-hunts.md)).

## Where DFE sits beside the warehouse you keep

A warehouse earns its place for finance, product and business analytics, where the data is modelled, the volume is modest and the joins are wide. Keep it. DFE takes the high-volume operational and security data a warehouse bills by the scan: device events, application logs, metrics, audit trails.

The two meet in object storage. dfe-archiver writes every record's payload as JSON lines to S3, GCS or Azure Blob, zstd by default, or gzip where the reader wants it, so a Spark job or a warehouse external table reads the files as they stand ([archiving](dfe-archiver/archiving.md)). ClickHouse reads Parquet and Iceberg in object storage directly, so a dimension table the warehouse owns is a query away from the events. And the DFE console, or any SQL client, reads the ClickHouse tables themselves.

## Doing it the DFE way

| You'd do this in a lakehouse | In DFE you do this |
|---|---|
| Define the table, or run `INFER_SCHEMA`, before the first load | Create a source from the catalogue ([sources](operate/sources.md)). Every field lands, a mapping is optional |
| Restart the loader when a producer adds a column | Nothing. The field lands in `_json` and is queryable. Promote it when it's hot |
| Rebuild the silver model when a field changes | Promote the field in the meta schema. New records land in the column, old ones stay readable |
| Watch bytes scanned, or the warehouse's running minutes | Size the cluster once. A query costs CPU you own, and the sort key prunes what it touches |
| Schedule a dbt test or a Databricks job to alert | Write a rule (SQL on a source) and run it on a schedule with a hunt |
| Partition by date and expire with a lifecycle policy | Set the table's retention and pick a storage model, local, tiered or object-backed ([storage](dfe-infra/deployment/storage.md)) |
| Tag rows with a tenant column and write views per customer | Every row carries `_org_id`, and ClickHouse row policies keep each organisation to its own rows |

## What maps to what

| If you know | In DFE |
|---|---|
| A table in Snowflake, a Delta table, a BigQuery table | A source, landed as its own ClickHouse table |
| Snowpipe, Auto Loader, a COPY job | dfe-receiver for pushed data, dfe-fetcher for APIs, databases and files |
| A `VARIANT` column, `_rescued_data`, a JSON column | The `_json` column, typed on read, promoted to a column when it's hot |
| A dbt model over bronze | The `dfe-schemas` definition, compiled to tables by dfe-engine |
| A warehouse, a SQL endpoint, slots | A ClickHouse cluster you run, or ClickHouse Cloud |
| An external table over S3 | dfe-archiver's files, and ClickHouse's own object storage readers |
