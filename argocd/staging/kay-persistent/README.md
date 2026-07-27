# Kay's persistent DFE test env (STAGED - apply parked)

An always-on dfe-engine + dfe-ui in namespace `dfe-kay` that Kay points her
front-end work at, auto-bumping when new GHCR images land. Staged here (a path
no appset reads) so applying is a deliberate step.

## What is here

- `kay-engine-app.yaml`, `kay-ui-app.yaml` - self-contained ArgoCD Applications
  (not the draft preview appset), GHCR registry, auto-sync, and
  argocd-image-updater annotations for auto-bump-on-new-image.

## Apply runbook (parked - needs the cluster + one install)

1. **Install argocd-image-updater** (not installed on the cluster today):
   `helm install argocd-image-updater argo/argocd-image-updater -n argocd`.
   Pin the version in `versions.yaml`. Give it GHCR read creds - it reads the
   `ghcr-pull-secret` referenced in the annotations (create it in `argocd` ns if
   absent). Without image-updater the apps still deploy; they just won't
   auto-bump - you'd bump `image.tag` by hand.

2. **Seed a starting tag**: set `image.tag` in each app to the current stable
   engine/ui GHCR tag on first apply (the charts leave tag empty otherwise).
   image-updater then advances it per the semver strategy.

3. **Apply**: `kubectl apply -f kay-engine-app.yaml -f kay-ui-app.yaml`, or move
   them under a watched path. Verify `kubectl -n dfe-kay get pods`.

4. **Access (Cluster B has no working L7 gateway yet)**: reach the UI via
   `kubectl -n dfe-kay port-forward svc/kay-dfe-ui 3000:3000` until the Envoy
   Gateway controller is stood up on Cluster B (currently unwired - see dfe-infra
   bootstrap). A hostname + route is a separate gateway task.

5. **For LIVE SSO e2e (separate prerequisite, not the deployment itself)**: the
   engine chart is still Envoy-header-trust shaped (`oidc.enabled: false`,
   native-RP-on-k8s not deployable per the OIDC gap analysis). To let Kay click
   through a real OIDC login she needs, on this env:
   - a session secret for the RP (SessionMiddleware),
   - a seeded `config/auth/oidc-providers/<provider>.yaml` (e.g. dex or
     google-workspace) mounted into the engine,
   - `NEXT_PUBLIC_OIDC_PROVIDERS` set on the UI so the SSO buttons render.
   Until then Kay can still test ROLE-BASED UI VISIBILITY with zero backend via
   the dfe-ui per-role test scaffold (rbacFixtures + rbacPerspective.test) -
   that path is already delivered and needs no cluster.

## Why staged

Applying mutates the shared cluster, image-updater must be installed first, and
live SSO needs the native-RP-on-k8s engine-chart work. All parked for a human
with cluster access.
