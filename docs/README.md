# dfe-infra documentation

Start here to deploy, test and secure the DFE suite with dfe-infra. It covers the architecture, the deployment guides under `deployment/`, the validation cycles and the threat model.

> **Coming from Splunk or Elastic?** DFE does several things differently on purpose. Read [coming from Splunk, Elastic or a lakehouse](../docs-site/content-root/coming-from.md) first.

- [The attended acceptance cycle](acceptance-cycle.md)
- [dfe-infra architecture](architecture.md)
- [Auth testing -- where the identities come from](auth-testing.md)
- [Autoscaling: the two layers and the per-target fork](autoscaling.md)
- [Keeping shared facts current](ci-maintenance-design.md)
- [ClickHouse identity](clickhouse-identity.md)
- [ClickHouse native JSON type](clickhouse-json-type.md)
- [Cloud cycles](cloud-cycle.md)
- [DFE cryptographic standard](cnsa-standard.md)
- [Cutting a stack version](cutting-a-stack.md)
- [Release + deploy helpers](deploy-helpers.md)
- [Trusting the DFE certificate](deploy-tls-trust.md)
- [Deploying DFE with dfe-infra](deployment/README.md)
- [Design principles](design-principles.md)
- [Edge exposure and authentication -- cloud specifics](edge-auth-cloud.md)
- [Edge exposure and authentication](edge-auth.md)
- [Readiness and failure evidence](failure-evidence.md)
- [The ingest edge](ingest-edge.md)
- [Release rounds](release-rounds.md)
- [The post-deploy source tests (`acceptance --suite source`)](source-suite.md)
- [DFE stack components - render class map](stack-components.md)
- [The DFE suite graph](suite-graph.md)
- [The validation cycle: create -> test e2e -> destroy](testing-cycle.md)
- [DFE exposure model](threat-model.md)
- [Use cases](use-cases.md)
- [Verification tiers -- T0 / T1 / T2](verification-tiers.md)
- [Why DFE](why-dfe.md)
