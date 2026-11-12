# ClickHouse native JSON type

> **Coming from Splunk or Elastic?** The `_json` columns in DFE's tables (`dfe.main._json`, for example) are this native type. A path nobody has mapped yet is queryable by name from the first event, and a type hint added through the `dfe-schemas` meta-schema gives it a fixed type. More in [what's different](../docs-site/content-root/coming-from.md#nothing-breaks-when-a-source-changes).

This is the current native `JSON` type, the one that replaced the experimental `Object('json')` in ClickHouse 25.x. The two behave very differently. `Object('json')` unified every path to a single least-common-type and used `col['key']` map access. The native type stores each path as its own typed sub-column with dot access. A lot of older docs, blog posts and answers still describe the old one, so double-check anything you carry over. Facts here are from the live ClickHouse docs. Re-verify the version-specific numbers against your server before relying on them.

The `JSON` type stores semi-structured data column-wise. Every JSON path is inferred and stored as its **own typed sub-column** on disk, up to `max_dynamic_paths` (default 1024). Paths beyond that spill into a shared `Map(String,String)`, which is slower to read (see the options table). So you query a field by its path and get back a real typed value, not a slice of serialised text. That makes `WHERE json.response_code = 200` a typed integer comparison instead of a string match against a blob. It replaces the deprecated experimental `Object('json')` / old `JSON` implementation entirely.

**Current as of ClickHouse 25.3+**, the release that marked the type GA.

## Why it exists (vs the old `Object('json')`)

The old `Object('json')` unified every value seen for a path into a single **least-common-type** for the whole column. A path that saw both `42` and `"hello"` collapsed to `String`, and a new incompatible value could force a whole-column rewrite. It had known bugs that won't be fixed.

The native `JSON` type stores **each path as its own sub-column**. Paths without a type hint are backed by the `Dynamic` type, which allows values with different data types for the same path **without unification into a least common type**. One path can hold an `Int64` in one row and a `String` in another, each stored natively and read back typed.

Version timeline:

- The new `JSON` type was introduced as experimental (behind `allow_experimental_json_type`), then beta.
- **Production-ready / GA in 25.3** (OSS): *"JSON data type is marked as production ready in version 25.3. It's not recommended to use this type in production in previous versions."*
- **`Object('json')` fully removed in v25.11** (backward-incompatible). Any table or query referencing `Object('json')` must be migrated (`ALTER TABLE ... MODIFY COLUMN ... JSON`) before upgrading past 25.11. *Confirm the exact removal release against the target changelog.*

## Declaration syntax

```sql
<column> JSON(
    max_dynamic_paths=N,
    max_dynamic_types=M,
    some.path TypeName,          -- typed path hint
    SKIP path.to.skip,           -- drop this path on parse
    SKIP REGEXP 'paths_regexp'   -- drop paths matching the regex
)
```

| Option | Purpose | Default |
|---|---|---|
| `max_dynamic_paths` | How many paths are stored as separate sub-columns (per block). Paths beyond this spill into a shared `Map(String,String)`, slower to read. | 1024 |
| `max_dynamic_types` | How many distinct data types a single `Dynamic` path column keeps separate. | 32 |
| `some.path TypeName` | Force a path to a fixed type (e.g. `user_id UInt64`). Typed hints read back as that exact type, not `Dynamic`. | - |
| `SKIP path` | Skip a path entirely during parsing. | - |
| `SKIP REGEXP 'rx'` | Skip all paths matching the regex. | - |

The docs say not to set `max_dynamic_paths` above ~10,000 on local filesystems, or 1024 on remote/object storage.

## Selecting a sub-column (typed access)

Access a path with dot notation:

```sql
SELECT json.a.b, json.c FROM test;
```

Type rules:

- A path with a **type hint** returns that exact type: `json.user_id` -> `UInt32`.
- An **undeclared** path returns `Dynamic`.
- A **missing** path returns `NULL` (JSON `null` and absent are equivalent here).

Paths are flattened, so `{"a":{"b":42}}` and `{"a.b":42}` both store as path `a.b`. They collide unless `json_type_escape_dots_in_keys=1` (25.8+), which escapes literal dots as `%2E` (access then looks like `` json.`a%2Eb` ``).

### Reading a nested sub-object - the `^` operator

```sql
SELECT json.^a.b, json.^d.e.f FROM test;   -- returns JSON objects, not scalars
```

### Casting a `Dynamic` path to a concrete type

```sql
SELECT json.a.g.:Float64, json.d.:Date FROM test;   -- typed sub-column read
SELECT json.a.g::UInt64 AS uint FROM test;           -- CAST equivalent
```

ClickHouse throws an exception if the `Dynamic` value can't be cast to the requested type.

### Arrays of objects

A JSON array of objects parses as `Array(JSON)`. Use `[]` to descend:

```sql
SELECT json.a.b[], json.a.b[].c.:Int64 FROM test;
```

## Filtering on sub-columns - TYPED, not string matching

```sql
-- RIGHT: typed integer comparison on the sub-column
SELECT count() FROM logs  WHERE json.response_code = 200;
SELECT *       FROM events WHERE json.user_id > 100;   -- typed-hint path
```

Casting rules for filters:

- Type-hinted paths compare natively, with no cast.
- `Dynamic` (undeclared) paths compare against the value inside the Dynamic. For a guaranteed concrete type (range compare, index, mixed-type path), read it typed: `WHERE json.status.:UInt16 = 200` or `WHERE json.status::UInt16 = 200`.
- Use `toString(json.path)` / `CAST` only when you really want the string form, **not** as the default way to filter.

The wrong, old-knowledge way is matching the serialised document (`WHERE toString(json) LIKE '%200%'`, or `JSONExtractInt` over a `String` column). It defeats columnar sub-column storage and scans the whole blob, which is exactly what the native type exists to avoid.

## Introspection - discovering the paths (path enumeration)

These functions discover what sub-columns a JSON column holds, for a UI that shows them or builds filters from them:

- `JSONAllPaths(json)` - every path in the row.
- `JSONAllPathsWithTypes(json)` - paths with inferred types.
- `JSONDynamicPaths(json)` - paths stored as separate dynamic sub-columns.
- `JSONSharedDataPaths(json)` - paths spilled into the shared structure (over `max_dynamic_paths`).
- `JSONAllValues(json)` - all values.

The `JSONExtract*` family (`JSONExtract`, `JSONExtractString`, `JSONExtractInt`, ...) works on **JSON strings** and navigates by index/key. On a native `JSON` column use **dot sub-column access** instead (`json.path`, `json.^path`, `json.path.:Type`).

## Gotchas

- No bracket / map access. `col['key']` and `arrayElement` on a `JSON` column are rejected. Use dot notation (`col.key`) only. This changed from `Object('json')`.
- Undeclared paths are `Dynamic`. `toTypeName(json.action)` returns `Dynamic`, not the leaf type. Cast when a downstream op needs a fixed type, or give the path a type hint in the DDL.
- `toString()` / `CAST` is needed when reading a mixed-type `Dynamic` path as one concrete type, forcing a comparison type, or serialising a nested object to text.
- `max_dynamic_paths` overflow silently moves extra paths into a slower shared `Map(String,String)`. Size the hint for high-cardinality path sets.
- Indexing / ORDER BY on sub-columns is supported. A JSON sub-column can go in a data-skipping index or, via a typed hint, a primary/partition key:

  ```sql
  CREATE TABLE sensor_data (
      data JSON(sensor_id UInt32),
      INDEX idx_sensor data.sensor_id TYPE minmax GRANULARITY 1
  ) ENGINE = MergeTree ORDER BY tuple();
  ```

  Also index by structure with `INDEX idx JSONAllPaths(data) TYPE bloom_filter`.
- Perf trade-offs the docs call out. INSERTs are slower (path splitting + type inference), reading *entire* objects is slower than a plain `String`, and many sub-columns add storage overhead. Use `JSON` when you query/filter/aggregate on specific paths. Keep `String` for an opaque blob you never field-query.

## Worked example

```sql
CREATE TABLE events
(
    id   UInt64,
    json JSON(user_id UInt32, timestamp DateTime, SKIP internal.debug)
)
ENGINE = MergeTree
ORDER BY id;

INSERT INTO events VALUES
  (1, '{"user_id": 123, "timestamp": "2024-01-01 10:00:00", "action": "login",  "response_code": 200}'),
  (2, '{"user_id": 456, "timestamp": "2024-01-01 10:05:00", "action": "logout", "response_code": 500}');

SELECT
    json.user_id,                          -- UInt32 (type hint)
    toTypeName(json.user_id)   AS uid_t,   -- 'UInt32'
    json.action,                           -- Dynamic (inferred)
    toTypeName(json.action)    AS act_t    -- 'Dynamic'
FROM events
WHERE json.user_id > 100                   -- typed integer compare, no cast
  AND json.response_code = 200;            -- filter on an inferred path, typed
```

Converting an existing `String` column of JSON text:

```sql
ALTER TABLE test MODIFY COLUMN json JSON;
SELECT json.a, json.b FROM test;   -- now typed sub-columns
```

## Check before you rely on it

- The `max_dynamic_paths=1024` / `max_dynamic_types=32` defaults and the local/remote sizing guidance shift across releases. Check the server version.
- The exact `Object('json')` removal release (v25.11) came from release-call material, not the data-type page. Confirm against the upgrade target's changelog.

## Sources (ClickHouse docs, 2026-08-20)

- <https://clickhouse.com/docs/reference/data-types/newjson> - primary: declaration, sub-column access, `^`/`.:Type` casting, SKIP, limits, indexing, perf, the 25.3 production statement.
- <https://clickhouse.com/docs/sql-reference/functions/json-functions> - `JSONAllPaths`/`JSONDynamicPaths` vs the string-oriented `JSONExtract*` family.
- <https://clickhouse.com/blog/a-new-powerful-json-data-type-for-clickhouse> - architecture: per-path typed sub-columns, `Dynamic` without least-common-type unification, replaces deprecated `Object('json')`.
