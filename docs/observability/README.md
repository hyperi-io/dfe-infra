<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      docs/observability/README.md
Purpose:   Index for how a DFE deployment watches itself.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# Observability

How a DFE deployment watches itself. The stack's OTLP lands in dfe-hyperdx on every deployment.

- [Provider logs in dfe-hyperdx](provider-logs.md) -- the cloud provider's logs (EKS audit, CloudTrail, a Rancher API server audit log) in dfe-hyperdx beside the stack's OTLP.

HyperDX's usage-stats task is off. Upstream defaults it on with its own ingest key, so an unset value would report team, user and source counts and hostnames to a third party, and the stack already self-monitors into the deployer's own ClickHouse. `usageStats.enabled: true` in the dfe-hyperdx values switches it on for a deployer who wants to support upstream.
