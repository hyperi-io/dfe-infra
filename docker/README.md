# Docker build contexts for utility images hosted in dfe-infra

`dfe-toolbox/` is its own family (base + per-cloud layers), built by
`.github/workflows/toolbox-build.yml` to `ghcr.io/hyperi-io/`, not by the
`docker/*/Dockerfile` loop above that pushes to JFrog -- see
`docs/deployment/toolbox.md`.
