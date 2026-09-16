# Docker build contexts for utility images hosted in dfe-infra

`dfe-toolbox/` is its own family (base + per-cloud layers), built by
`.github/workflows/toolbox-build.yml` to `ghcr.io/hyperi-io/` -- see
`docs/deployment/toolbox.md`.

It is the only family here. A second workflow used to build `docker/*/Dockerfile`
to JFrog; it was retired because that glob matched no file, the estate publishes
to ghcr.io, and all four of its runs died at a login against an empty registry.
