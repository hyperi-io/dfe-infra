# Autoscaling: the two layers and the per-target fork

Two independent layers scale a DFE deployment. KEDA scales the pods the same way on every target, and the node autoscaler that turns Pending pods into nodes differs by target.

```mermaid
---
config:
  theme: base
  look: neo
  fontFamily: Inter, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif
  flowchart:
    curve: linear
    nodeSpacing: 40
    rankSpacing: 64
    padding: 18
    wrappingWidth: 280
    htmlLabels: true
  elk:
    preset: modelOrder
  themeVariables:
    fontSize: 15px
    lineColor: "#64748B"
    primaryTextColor: "#0F172A"
    nodeBorder: "#334155"
    clusterBorder: "#0072B2"
    edgeLabelBackground: "#FFFFFF"
  themeCSS: >-
    p{margin:0}
    .node[data-look] rect,.node[data-look] path,.node[data-look] polygon,.node[data-look] .outer-path{filter:drop-shadow(0 1px 2px rgba(15,23,42,.10)) drop-shadow(0 6px 14px rgba(15,23,42,.10))}
    .node rect{rx:10px;ry:10px}
    .cluster rect{rx:16px;ry:16px}
    .node .label div{min-width:150px}
    .node.decision .label div{min-width:0}
    .cluster[data-look] rect{filter:none}
    .nodeLabel{line-height:1.35}
    .nodeLabel b{font-weight:600;font-size:15px;letter-spacing:-0.01em}
    .nodeLabel span{font-size:12.5px;font-weight:400;color:#334155}
    .cluster-label span.nodeLabel{display:inline-block;background:#FFFFFF;color:#0B2A3F;border:1.5px solid #0072B2;border-radius:999px;padding:2px 12px;font-size:11.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}
    .flowchart-link{stroke-width:1.75px}
    .labelBkg{background:transparent!important}
    span.edgeLabel{display:inline-block;background:#FFFFFF!important;color:#1E293B;border:1px solid #94A3B8;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:500}
    span.edgeLabel p{background:transparent!important}
    .label-icon{width:20px;height:20px;vertical-align:-4px;margin-right:6px}
    .icon-shape path[stroke-width="0"][fill="none"]{fill:#FFFFFF;stroke:#FFFFFF;stroke-width:12px;stroke-linejoin:round}
    .icon-shape p{background:transparent!important;color:#0F172A}
    .ingest .label-icon{color:#0E8A7D}
    .xform .label-icon{color:#7B4FB0}
    .load .label-icon{color:#0072B2}
    .bus .label-icon{color:#A8517F}
    .store .label-icon{color:#00785A}
    .ui .label-icon{color:#D9480F}
    .ext .label-icon{color:#64748B}
---
flowchart TB
    accTitle: L4 Detail -- Autoscaling layers
    accDescr: On every target the HPA KEDA drives reads a CPU trigger and a pressure trigger through dfe-keda-shim and takes the higher. Pods it cannot place go Pending, and the node layer adds nodes with a static pool on-prem, Karpenter on AWS or cluster-autoscaler as the portable fallback.
    subgraph pods["Pod scaling, every target"]
        direction LR
        cpu("<b>CPU trigger</b>")
        shim("<b>Pressure trigger</b><br/><span>via dfe-keda-shim</span>")
        hpa("<b>HPA</b><br/><span>takes the higher</span>")
        cpu --> hpa
        shim --> hpa
    end
    subgraph nodes["Node scaling, per target"]
        direction LR
        static("<b>Static pool</b><br/><span>on-prem</span>")
        karpenter("<b>Karpenter</b><br/><span>AWS</span>")
        ca("<b>cluster-autoscaler</b><br/><span>fallback</span>")
        static ~~~ karpenter ~~~ ca
    end
    pods -->|pods Pending| nodes
    class cpu,shim,hpa,static,karpenter,ca part
  classDef ext fill:#F4F6F8,stroke:#94A3B8,stroke-width:1.5px,stroke-dasharray:5 4,color:#0F172A
  classDef ingest fill:#FFFFFF,stroke:#0E8A7D,stroke-width:3px,color:#0F172A
  classDef xform fill:#FFFFFF,stroke:#7B4FB0,stroke-width:3px,color:#0F172A
  classDef load fill:#FFFFFF,stroke:#0072B2,stroke-width:3px,color:#0F172A
  classDef bus fill:#FFFFFF,stroke:#A8517F,stroke-width:3px,color:#0F172A
  classDef store fill:#FFFFFF,stroke:#00785A,stroke-width:3px,color:#0F172A
  classDef ui fill:#FFFFFF,stroke:#D9480F,stroke-width:3px,color:#0F172A
  classDef decision fill:#FFFFFF,stroke:#9A6700,stroke-width:3px,color:#0F172A
  classDef error fill:#FFFFFF,stroke:#C2410C,stroke-width:3px,color:#0F172A
  classDef control fill:#FFFFFF,stroke:#334155,stroke-width:3px,color:#0F172A
  classDef part fill:#FFFFFF,stroke:#94A3B8,stroke-width:1.5px,color:#0F172A
  style pods fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
  style nodes fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 1. L4 Detail -- Autoscaling layers.* Up: [dfe-infra documentation](README.md).

The AKS and GKE node renderers are planned in dfe-infra#806, so the figure leaves them out. Node scaling forks by target, and that fork is a DECLARED decision, recorded here, never an assumption baked into a chart.

## Pod scaling

Each app's thin chart (scalo-rs `charts/scalo-service`) ships its ScaledObject, and dfe-extras ships the hunt runner's. On-prem and cloud run the same ScaledObjects.

Two triggers render and the HPA takes the higher. The app charts ship `keda.cpu` (the native scaler, nothing of ours in the path) and a `metrics-api` entry in `keda.extraTriggers` (`argocd/values/apps/_fragments/pressure-trigger.yaml`). That entry is the scalo ScalingPressure composite, read through the fail-safe dfe-keda-shim. A tier or an instance drops it with `keda.extraTriggers: []`.

- The shim matches the bare `scaling_pressure` gauge and any `<prefix>_scaling_pressure` for that ServiceName (dfe-engine `scaling_pressure.py`), so an app's metrics namespace needs no app release.
- The shim address comes from the release namespace, so a deployment in any namespace reaches it without a values override. `keda.pressure.shimAddress` is for a shim outside the release namespace.
- The three transforms have the pressure trigger in their single, scale and mesh profiles. Their slim profiles have none, so cpu is the one trigger there.
- The hunt runner scales on its due-hunt backlog through the same shim. That trigger is beta and off by default: `huntRunner.autoscaling.enabled` in the dfe-engine values turns it on.

`bootstrap/keda-scale-test.sh` tests the path on each deploy. It injects pressure for a throwaway Deployment, then for every app whose ScaledObject has the shim trigger at once, and each goes from its floor to one above and back. Its scale-out bound is the HPA sync period twice over plus a pod-start allowance, because above a floor of zero the HPA reads the metric itself and KEDA's pollingInterval plays no part. At scale-out it logs each HPA reading and fails when the pressure reading is under target, so a cpu-driven scale can't pass for a pressure one. It fails an app that wrote no `scaling_pressure` rows, because the shim reads no rows as 0 and that app scales on cpu alone. UNPROVEN (exit 3: the HPA read the pressure over target and left the replicas alone, or an app was off its floor before the run) fails the smoke run like a FAIL does.

## Which trigger each tier gets

> **Coming from Splunk or Elastic?** DFE scales pods on a composite pressure signal beside the CPU trigger. The composite gates to 0 when the downstream sink's circuit is open and forces 100 over the memory threshold, which CPU alone can't see.

CPU at 70% is the baseline everywhere and never comes out. It's native KEDA with nothing of ours in the path, so a component outage can't take scaling with it. Each tier decides whether the composite renders beside it.

| Tier | Pressure | Ceiling | Why |
| ---- | -------- | ------- | --- |
| slim | off | 2 | No broker and a 2-replica ceiling. CPU is the whole signal worth having, and the shim isn't worth putting in the path for it. |
| single | on | 3 | Has a broker, so `kafka_lag` measures the backlog an I/O-bound loader's CPU hides. The ceiling is the partition count the kafka chart derives for one broker (3), because a 4th consumer joins the group and gets nothing. |
| mesh | on | 10 | No broker, but every stage holds what the next hasn't taken, so `buffer_depth` and `memory` stand in for `kafka_lag`. Replicas share load through the Service, so no partition count bounds the ceiling. |
| scale | on | 12 | Has a broker. The ceiling is a DIVISOR of the derived partition count (12) so every reachable replica count holds the same number of partitions. At 10, two consumers hold 2 and eight hold 1, and the busy pair sets the group's lag. |

Why the composite pressure and not KEDA's native `kafka` scaler? The native scaler reads consumer-group lag from the broker and needs no shim. It's blind to the two things that decide whether another pod helps. The composite gates to 0 when the downstream sink's circuit is open (more loaders can't relieve a dead ClickHouse) and forces 100 over the memory threshold (scale before the OOM). Lag alone would scale the loader out against a sink that is refusing writes, and the composite has the lag term in it. A deployment that wants the native scaler can have it: `keda.triggers` takes a verbatim scaler list the chart passes straight through.

The thresholds are inherited. 70 on both the CPU trigger and the 0-100 composite came across from the fleet default with no load run behind it. Sizing them needs a run with more source topics or more partitions on `default_land` than a single-source rig has, because replicas 4-12 would otherwise idle.

## The fork decision for DFE deployments

> **Coming from Splunk or Elastic?** Node scaling isn't one mechanism here. KEDA scales the pods on every target, and the node autoscaler is chosen per target, with a static node pool on-prem because Karpenter needs a cloud provisioning API.

| Target | Node autoscaler | Notes |
| ------ | --------------- | ----- |
| On-prem Rancher/RKE2 (the baseline) | **None: static node pool.** cluster-autoscaler IF the node layer gains a provisioning API (Rancher node pools on a cloud provider, Harvester, etc.) | Karpenter is NOT an option here, because it needs a cloud provisioning API. A fixed node pool is the sovereign and air-gap default: capacity planning replaces node autoscaling, and KEDA still does the pod side. |
| AWS EKS | **Karpenter**, the node-pools chart's `karpenter-aws` renderer | The one renderer built. |
| Azure AKS | **Node Auto Provisioning** (managed Karpenter) | A planned renderer, dfe-infra#806. |
| GCP GKE | **ComputeClass plus OpenTofu node pools** | A planned renderer, dfe-infra#806. GCP has no supported Karpenter. |
| Multi-cloud / mixed | **cluster-autoscaler** | The portable fallback. Runs everywhere. |

One rule covers every component whose best implementation differs by target (node autoscaler, gateway controller, secret store, load balancer, storage class): state the choice per target, keep the chart neutral with the choice in values or the target overlay, and name the portable fallback (here cluster-autoscaler) so no target is stranded.

Node scaling is a pools interface. The resolver writes one pool class per workload, in fields every node autoscaler shares, and `helm/charts/node-pools` hands the classes to the renderer `pools.renderer` names. Karpenter on AWS (`karpenter-aws`) is the one renderer today. The AKS and GKE renderers are planned in dfe-infra#806.

`argocd/appsets/layer2-platform.yaml` deploys the pools chart only to a cluster whose secret declares a cloud with a renderer (AWS today), so the on-prem baseline gets no pool object at all. Every other chart places a pod by the pool's `dfe.hyperi.io/workload` label and the cloud-neutral node labels, never by Karpenter's or a cloud's own (`scripts/tests/test_charts_select_only_the_pool_label.py`).

## Hunt schedule smoothing

Status: beta from 2.2.3, off by default. Set `huntRunner.smoothing.enabled: true` in the deployment's dfe-engine values to turn it on.

Hunts on one interval start at hash-chosen offsets inside it, so a busy interval can stack several heavy queries into the same few seconds. With smoothing on, one hunt runner leads each cycle, reads each hunt's past cost, and moves rate-hunt offsets in `hunt_schedule` to flatten the load. A runner that starts with smoothing off puts every offset back on the hash. The chart hands the same switch to the engine, and the engine API reports next due from the learned offsets.

On a 3-replica ClickHouse test cluster, with 30 hunts whose offsets all fell in one 30 s window, smoothing took the summed peak memory of the busiest 30 s from 8.07 GB to 3.30 GB over three hours, with no errors. It is not proven at PB/day. It makes one move per cycle across the fleet, so a large fleet settles over hours to days at the defaults.

The tunables sit beside the switch under `huntRunner.smoothing` (`cycleSeconds`, `bucketSeconds`, `windowDays`, `costQuantile`, `maxStepFraction`, `hysteresis`, `cooldownCycles`, `weightMemory`, `memoryCapBytes`, `horizonMaxSeconds`). An empty one takes the engine's default, and a value the engine can't parse fails the render. The runner exports `dfe_hunt_smoothing_flatness`, `dfe_hunt_observed_mem_peak_bytes` and `dfe_hunt_observed_overlap_peak`.

## Hunt auto-disable

Status: on by default from 2.2.3, in the engine. The chart renders nothing until you set `huntRunner.autodisable.enabled`, `failures` or `windowSeconds` in the deployment's dfe-engine values.

A hunt that busts a ClickHouse resource limit (memory, rows or quota) fails its neighbours too, and every retry fails again. The hunt runner counts those failures per hunt, and after `failures` of them inside `windowSeconds` (3 in 3600 s at the defaults) it stops the hunt. Other failures, such as a syntax or connection error, are not counted.

The stop is its own `hunt_state` row keyed `<hunt_id>#stopped`, so it survives a runner restart. The hunt list and detail show `auto_disabled_at` and `auto_disabled_reason` (`resource_limit:<code>`). A PUT on the hunt with `enabled: true` clears it. The runner counts each stop in `hunt_autodisabled_total{hunt_id,reason}`.

`huntRunner.autodisable.enabled: false` turns it off, and an empty value takes the engine's default. Only the hunt runner reads these, so the engine pod gets none of them. A value the engine can't parse fails the render.
