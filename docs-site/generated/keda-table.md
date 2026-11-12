| App | Replicas, slim | Replicas, single | Replicas, scale | CPU target | Pressure trigger on |
|---|---|---|---|---|---|
| `dfe-archiver` | 1-2 | 1-3 | 2-12 | 70% | single, scale |
| `dfe-loader` | 1-2 | 1-3 | 2-12 | 70% | single, scale |
| `dfe-receiver` | 1-2 | 1-3 | 2-12 | 70% | single, scale |
| `dfe-transform-elastic` | 1-2 | 1-3 | 2-12 | 70% | single, scale |
| `dfe-transform-vector` | 1-2 | 1-3 | 2-12 | 70% | no |
| `dfe-transform-vrl` | 1-2 | 1-3 | 2-12 | 70% | single, scale |
| `dfe-ui` | 1-2 | 1-3 | 2-12 | 70% | no |
