# Readiness and failure evidence

What the cycle's readiness gate refuses, and what a failed stage leaves behind to read. The cycle itself is [testing-cycle.md](testing-cycle.md).

## What the readiness gate refuses

Beyond pod readiness and replica counts, the gate asks the engine's `/api/v1/auth/setup-status` whether the deployment is running the shipped admin password. It FAILS when it is and `DFE_ENGINE_ENV` isn't one of the dev values (`dev`, `development`, `local`, `test`, `ci`). Ready isn't the same as safe: the deploy mints `admin` and `breakglass` through ESO Password generators, so a stack still on the default hasn't taken them.

An engine too old to serve `default_credentials` answers without the field, and that only warns. The probe reached the engine and only the field is missing. A probe that could not RUN at all (an RBAC denial on `kubectl exec`, a wrong `READINESS_ENGINE_TARGET`, a non-200 from setup-status) leaves the gate unable to say whether the shipped password is in use, so it FAILS outside the dev values and warns inside them.

It then runs `dfe-ops admin-probe`, which fetches every admin UI in the `dfe-admin-links` ConfigMap through the gateway address under its own hostname, following redirects. A redirect loop, a 5xx or no answer FAILS in every environment, because a Ready pod behind a looping route is still a UI nobody can open. So does a 4xx other than 401, 403 or 404 at the end of the chain, such as an IdP answering 400 to a redirect URI it hasn't registered. A redirect off the deployment's domain is a login handed to an IdP and passes. No ConfigMap, no Gateway address, or a gateway this machine can't reach is a named SKIP. A failing UI is re-probed for `READINESS_ADMIN_UI_WAIT` seconds (default 120) before it fails the gate.

When the gate times out it runs `dfe-ops readiness-report` once, because a ready count can't tell a pod with nowhere to schedule from one that can't pull its image. For every pod not Ready in a judged namespace or kube-system, the report prints the scheduler's verdict and each container's wait. Then it prints the newest Warning events, every node's placement labels, taints and requested-against-allocatable CPU and memory, Karpenter's NodePools, NodeClaims, EC2NodeClasses and controller errors, unbound volume claims, and Argo Applications not Synced and Healthy. It reads and never writes, and it never changes the gate's exit code.

`dfe-ops creds` prints where each minted credential is fetched from, reading both Secret names off the live engine Deployment.

## When a stage fails

The cycle runs `dfe-ops failure-evidence` once before the destroy, so the evidence outlives the cluster. It writes one file per capture into a timestamped directory under `--evidence-dir` (default `failure-evidence/` under `--acceptance-shots-dir`). The captures:

- `nodes -o wide`, each node's conditions and allocatable, and `pods -A -o wide`.
- Every namespace's Warning events oldest first, and kube-system events oldest first.
- Services and Gateways `-A -o wide` (a load balancer never programmed shows as a Service with no address).
- The cert-manager Certificates in kube-system, and a summary.
- The events and container logs of each kube-system pod not Running and Ready.
- The same for up to 40 pods in any namespace with a container waiting on `ImagePullBackOff`, `ErrImagePull`, `CrashLoopBackOff` or `CreateContainerConfigError`, or with an init container not yet finished, then each pod that is Running but not Ready, such as one whose readiness probe fails (the events carry the kubelet's pull or config error, and a not-Ready pod past the 40 is named on a closing `+N more not-Ready pods` line).
- The AWS Load Balancer Controller's webhook configurations with each `caBundle` reduced to a digest, and its containers' newest log lines.
- The events of each node reporting `InvalidDiskCapacity`, every aws-node container's newest log lines, and the CNI's `ipamd.log`, `plugin.log` and `network-policy-agent.log`.
- What an ESO re-mint leaves behind: every ExternalSecret's sync status, their Normal events, the external-secrets controller's newest log lines, and the creation time, `resourceVersion` and `immutable` flag of each Secret a generator writes, read by those field names alone.

A pod summary comes from the pod list already read: node, phase, conditions, and for every init and main container its image, state, restart count and last termination. It leaves out every container's env, envFrom, args and command, the pod's volumes and its annotations. Nothing runs `kubectl describe`, because that prints literal env values into an artefact CI uploads. Each of those pod reports also carries the containers' logs: the previous run of one that restarted or sits in `CrashLoopBackOff`, a running or failed init container, and a running container that isn't ready. A read keeps the newest 200 lines within 64 KiB, and all pod logs of a run stop at 8 MiB. Every log passes through a redactor first, which masks values under credential-named keys, URL userinfo, bearer tokens, private key blocks and common token shapes. `index.txt` names the command behind each file and how it ended. Each call is capped at 30 seconds and 1 MiB and the whole capture at 300 seconds. A failing call never stops the rest, nothing reads a Secret's data, and the cycle's verdict doesn't change.

## Diagnostics bundle

With `--diagnostics-dir DIR`, a failed stage from `deploy+e2e` on runs `dfe-ops diagnostics` before the destroy, as does the source suite before it removes its source and that source's transform pods. Each run writes `DIR/<UTC time>-<label>/`:

- `logs/` (the receiver, loader, archiver, engine, hunt runner, dfe-hyperdx, the DocumentDB store, every fetcher and every transform, read back to the failed stage's start or 30 minutes, whichever is longer).
- `workloads.txt`, `events/` and `argo-applications.txt`.
- `kafka/` (group and topic offsets, broker API versions, the loader group read step by step).

The Kafka reads come from a short-lived pod that mounts the engine's own SASL Secret, so no Secret is read. On `msk` it copies MSK's broker logs from S3 into `kafka/broker-logs/`, and S3's batching can leave out the last few minutes. Every file is redacted, the whole capture stops at 300 seconds, and a capture that fails is recorded in `index.txt` while the rest still run.

That bundle sees only the pods still alive. A fetcher or transform the suite removed, or the crashed run of a restarted container, is gone by then. So the acceptance stage runs `dfe-ops pod-logs` on the app namespace for its whole run, and keeps `DIR/<UTC time>-acceptance-pod-logs/` when the stage fails. Each container run gets its own file, `<namespace>/<pod>.<container>.<run start>.log`, written as it runs, and `index.txt` says how each stream ended.

A run keeps its first and newest MiB, a pod 8 MiB and the stage 128 MiB. It reads through a `kubectl proxy` on a private Unix socket that admits only a GET of that namespace's pods and pod logs, so no Secret is reachable, and it redacts every line. It stops with the stage however the stage ends, and by itself after six hours.
