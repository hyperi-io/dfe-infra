# Migrating from DFE 2.x before 2.2

A private-cloud DFE deployment run before 2.2 used an internal ClickHouse fork with its own SSD cache in front of bulk storage, under an internal operator (described, not named, in [clickhouse.md](clickhouse.md)). DFE 2.2 retired that pairing for less code to maintain and one storage model across on-prem and cloud.

The replacement is the official ClickHouse operator with the `cached-object` storage model: an object-store disk with a local cache disk in front of it, under the `s3_cached` policy. `cached-object` is the default wherever an object-store endpoint exists (`sizing.storage_model: auto`), and `local` is the explicit opt-out.

A 2.1 to 2.2 move is a fresh deploy plus a data migration, with a second deployment beside the first. `clickhouse.storageModel` is a protected var for the life of a deployment (see [clickhouse.md](clickhouse.md)), so the storage model can't flip under the running one. Stand up a DFE 2.2 deployment with an object-store endpoint: a cloud bucket on AWS, or on-prem an S3-compatible endpoint such as MinIO ([storage.md](storage.md) lists the flavours). Run it alongside the old one until data has moved and the cutover is proven.

Move the data by ClickHouse `BACKUP`/`RESTORE` to the object store where the source server supports it. This repo carries no documented replay path from dfe-archiver back into ClickHouse, so otherwise the re-ingest comes from your own retained raw stream and is a customer-specific plan. Once the new deployment holds the required retention window, cut the senders over to its receiver address, then decommission the old one.

Plan separately for schema differences between the fork's engine and upstream MergeTree, any table or setting the fork added, and a retention window longer than the cutover overlap. During the overlap, compare the old and new deployments using your heaviest queries.

## Payload format

JSON is the only payload format. MessagePack, supported in DFE/XDR 2.0 and 2.1, is no longer accepted in DFE 2.2, and a MessagePack record is dead-lettered as not JSON. The loader's `payload.format` and dfe-transform-vrl's `source.format` settings are gone. Fluentd input over the Fluent Forward protocol is unaffected: the receiver turns it into JSON.
