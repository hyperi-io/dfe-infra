# OpenTofu

`environments/<cloud>/` holds one root per cloud, and `modules/<capability>/<body>/` one body per implementation, each with its own `CONTRACT.md`.

## No sizing literals in a `.tf` file

- An instance type, a node or broker count, a volume type, size, IOPS or throughput, a retention period, a CIDR, a region or zone list, and an engine or Kubernetes version never appear as a literal in a resource, a module call, a local or a variable default.
- An instance type is auto-selected by the shape resolver (`scripts/resolve_sizing.py`), newest arm64 generation in the region, and reaches tofu as `resolved_shapes` in `sizing.auto.tfvars.json`.
- A type with no dependable resolver is pinned in tfvars, or in the shapes data that feeds them, with a one-line reason.
- Every other value arrives from tfvars, rendered from the deployment dial by `scripts/render_dial.py` or written into a hand-kept tfvars file.
- Each such variable's `description` says which of the two its value comes from.
- A literal the cloud's own API fixes -- a managed policy name, a protocol port, a service principal, an image family -- stays in the `.tf` as a named local with a one-line comment saying why.

## Where each cloud's types come from

`shapes/compute-shapes.yaml` names the source per use case:

| Cloud | Node and instance types | Managed broker | Resolver status |
|-------|-------------------------|----------------|-----------------|
| aws | `ec2` -- the EC2 and Pricing APIs | `msk-computefamily` -- MSK's own compute families | populated |
| gcp | `compute-machine-types` | `vendor-overlay` | stub: arm64 and `newest` are set, no family is named and nothing reads the source yet |
| azure | `vm-sizes` | `vendor-overlay` | stub: arm64 and `newest` are set, no family is named and nothing reads the source yet |
| onprem | `node-requirements` -- a table the cluster's own nodes must meet, amd64 | `node-requirements` | stub |

On-prem nodes are not created here, so the resolver emits requirements rather than a type, and the toolbox runs as an in-cluster pod (`in-cluster-pod`).
