---
sidebar_position: 7
sidebar_label: Parquet and Iceberg
---

# Parquet and Iceberg for other tools

The console, the rules and the hunts read DFE's tables in ClickHouse. When Spark, Trino, DuckDB or a warehouse needs that data too, ClickHouse SQL writes the columns you select as Parquet files or into an Iceberg table in object storage. DFE doesn't schedule these exports. You run them from the scheduler you use for everything else.

An exported file has no row policy. Every source table has an `_org_id`, so filter each export to one organisation's tenant ids, write it to a path of its own, and grant the readers that path alone in the object store ([ClickHouse identity](../dfe-infra/clickhouse-identity.md)).

`_json` holds the record's JSON payload, the fields nobody has promoted included, as ClickHouse's JSON type. Parquet can't take that column as it stands, so cast it to a string (`_json::String`), or select the paths you want as columns. Add your promoted columns to the `SELECT` beside it.

## Parquet files

The `s3` table function writes the result of a query as Parquet, compressed with zstd by default ([s3 table function](https://clickhouse.com/docs/sql-reference/table-functions/s3), [Parquet format](https://clickhouse.com/docs/interfaces/formats/Parquet)). `azureBlobStorage` does the same on Azure. This writes one organisation's rows of a source that DFE loaded yesterday, partitioned by load day:

```sql
INSERT INTO FUNCTION s3(
    'https://<bucket>.s3.<region>.amazonaws.com/dfe/<org>/firewall',
    '<access key id>', '<secret access key>',
    'Parquet',
    partition_strategy = 'hive')
PARTITION BY (load_day)
SELECT
    toDate(_timestamp_load) AS load_day,
    _timestamp,
    _org_id,
    _source,
    _json::String AS _json
FROM dfe.firewall
WHERE _org_id = '<tenant id>'
  AND _timestamp_load >= today() - 1 AND _timestamp_load < today();
```

The `hive` strategy writes `load_day=<date>/` directories, which Spark, Trino and DuckDB prune on. The window is on `_timestamp_load`, when the row landed in ClickHouse, so an event that arrives late goes out in the next run rather than being missed. A run that fails, or never runs, leaves its day out, so keep a record of the days exported and rerun any gap. ClickHouse writes new files and never appends to one it wrote earlier, so a rerun writes the day's rows a second time. Delete that day's directory before you retry it.

## Iceberg tables

An Iceberg table gives the readers a schema, snapshots and time travel on top of the Parquet files. ClickHouse creates and writes one with the `IcebergS3` table engine (`IcebergAzure` on Azure) once `allow_insert_into_iceberg` is on. Read ClickHouse's notes on Iceberg writes before you rely on them. They list the types it can't write, `UInt64` among them ([Iceberg table function](https://clickhouse.com/docs/sql-reference/table-functions/iceberg), [Iceberg table engine](https://clickhouse.com/docs/engines/table-engines/integrations/iceberg)).

```sql
SET allow_insert_into_iceberg = 1;

CREATE TABLE firewall_iceberg
(
    _timestamp DateTime64(3),
    _org_id String,
    _source String,
    _json String
)
ENGINE = IcebergS3(
    'https://<bucket>.s3.<region>.amazonaws.com/iceberg/<org>/firewall/',
    '<access key id>', '<secret access key>');

INSERT INTO firewall_iceberg
SELECT _timestamp, _org_id, _source, _json::String
FROM dfe.firewall
WHERE _org_id = '<tenant id>'
  AND _timestamp_load >= today() - 1 AND _timestamp_load < today();
```

A rerun inserts the same rows again, so retry a failed window only once you know what it wrote.

`IcebergS3` writes the Iceberg metadata straight to the bucket, with no catalog in the loop. Spark, Trino and DuckDB can open the table by its path. A catalog that registered it keeps the snapshot it registered, so point the catalog at the newest metadata file after each export.

## Reading the warehouse's tables from ClickHouse

The other direction needs no copy. The `DataLakeCatalog` database engine connects ClickHouse to an Iceberg REST catalog, AWS Glue or Unity Catalog, and a query reads those tables where they sit ([DataLakeCatalog](https://clickhouse.com/docs/engines/database-engines/datalakecatalog)). A dimension table your warehouse owns joins to DFE's events in one ClickHouse query.

## Raw payloads

Where dfe-archiver runs, it writes each payload exactly as it arrived, as JSON lines in S3, GCS or Azure Blob ([archiving](https://github.com/hyperi-io/dfe-archiver/blob/main/docs/archiving.md)). Spark and the warehouses read those files as an external table, for as long as your bucket keeps them.
