<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      docs/observability/README.md
Purpose:   Index for how a DFE deployment watches itself.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# Observability

How a DFE deployment watches itself. The stack's own OTLP lands in HyperDX on every deployment.

- [Provider logs in HyperDX](provider-logs.md) -- the cloud provider's own logs (EKS audit, CloudTrail, a Rancher API server audit log) in HyperDX beside the stack's OTLP.
