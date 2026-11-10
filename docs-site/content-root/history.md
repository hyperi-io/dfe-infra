---
sidebar_label: How we got here
slug: /history
description: DFE 2.2 is the first open-source release of a platform in production since 2018, which is why public versions start at 2.2.
---

# How we got here

DFE 2.2 is the first open-source release. It isn't the first release. The platform has run in production since 2018, so the public version numbers start where the platform already was.

Here's the timeline, newest first.

- 2026 -> DFE 2.2 and Edge Stream Hub go open source. Support subscriptions and bring-your-own-cloud licensing replace SaaS and PaaS first. Our ClickHouse fork retires from customer deployments, because DFE now does that work on the way in.
- 2024-2025 -> XDR becomes DFE, because the data was never only security. DFE 2.1 starts swapping general-purpose pipeline parts for purpose-built Rust apps, beginning with the loader. 2.1 runs at scale in large organisations.
- 2021-2023 -> XDR 2.0 and HyperCollectors 2.0. OpenSearch replaces Elastic as the quick-look sidecar after Elastic's licence change. Vector is plumbed in stack-wide, and HyperCollectors install as packages with standard Linux and Windows integrations. We fork ClickHouse for nulls, sparse data and tiered caching.
- 2020 -> ClickHouse replaces Apache Kudu as the main store, once it handles nulls and sparse data well enough.
- 2018 -> XDR (now DFE) and HyperCollectors (now Edge Stream Hub). XDR runs on Kudu, which we extend for caching, schema control and a JSON base column. DFE still uses that approach, now on ClickHouse's JSON type. It's sold as SaaS and PaaS to enterprise and government, plus services.
- 2017 -> Derek and Damien found HyperI, coming from government, big finance and defence, to do cyber analytics at scale.

## Version numbers

- DFE starts at 2.2 in public because XDR 1.x and 2.x, then DFE 2.1, shipped to customers privately.
- Every DFE app carries its own version. Apps that have been around a while are past 1.0.
- Newer apps are below 1.0, but they aren't alpha or beta. We keep work private until it's mature.
- A DFE release such as 2.2.2 pins one tested version of every app. [Stack versioning](dfe-infra/deployment/stack-versioning.md) has the detail.
