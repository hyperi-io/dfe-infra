# Verification tiers -- T0 / T1 / T2

How we prove a DFE version works. The proof surface is a MATRIX, and these three
tiers name how much of it a given check covers. The terms are canonical: use
T0/T1/T2 in issues, PRs, commit bodies and here, not ad-hoc phrases.

## The matrix

Two axes. A cell is one (target x data-path) pair streaming end to end into
ClickHouse and read back.

- **Targets** -- where the stack runs:
  - **a. local** -- each interdependent repo cloned and run as a process on a
    dev machine (NOT a container, NOT k8s), against a local `dfe-deploy` clone,
    backed by the capped local ClickHouse + Kafka. See
    [each repo's local-dev doc](#local-dev).
  - **b. docker** -- the `dfe-docker` compose profiles.
  - **c. k8s** -- dfe-infra (RKE2 + ArgoCD), across the three profiles
    `slim` / `single` / `scale`.
- **Data paths** -- what flows:
  - **otel** -- the OpenTelemetry path (collector -> `dfe.otel_*`).
  - **dfe.default** -- the receiver/loader event path (Kafka -> `dfe.default`).

BOTH paths must land rows in ClickHouse and be read back. A `200` from a Service
is not proof; driving the product is (see [Driving the product](#driving)).

## The tiers

### T0 -- ITERATIVE (the daily inner loop)

ONE persistent canonical deployment we NEVER tear down: the `scale`-profile
deployment left running on the devex k8s cluster -- the one that survives the
slim -> single -> scale sweep -- with stable DNS and stable logins. This is the
single canonical manual-verify location AND the canonical rig; dfe-b vs devex
stops mattering once it stands.

- Fast check = the CORE data-path smoke (seconds): a handful of events down each
  path, rows confirmed in ClickHouse.
- UI work drives Playwright here against real streaming data.
- **Why `scale`, not `single`:** scale has the most moving parts (a ClickHouse
  CLUSTER, HA Kafka) and surfaces bugs a single node hides -- a test green on
  single-node ClickHouse can fail against a cluster (distributed DDL, `ON
  CLUSTER`, keeper). We kick the tyres on the config with the most that can go
  wrong.

The CORE smoke against the standing rig (uses the current kube context):

    python3 scripts/dfe-ops verify

### T1 -- PER-TARGET (tens of minutes each)

One target, both data paths, end to end. Run the one that matches what you
changed.

- **T1a local** -- boot every interdependent repo as a process, both paths. The
  procedure is [dfe-engine `docs/LOCAL-DEV.md`](#local-dev) plus each repo's own
  local-dev doc; the local ClickHouse + Kafka come from the capped
  `local-services` daemons.

- **T1b docker** -- `dfe-docker` up, both paths (its `make dev` / `test_e2e`).

- **T1c k8s** -- `dfe-ops cycle` for the profile: preflight -> deploy + the two
  E2E paths -> smoke -> teardown, then Playwright against the standing rig.

      python3 scripts/dfe-ops cycle --mode single \
          --kubeconfig .tmp/target.kubeconfig --env-file bootstrap/.env

### T2 -- FULL (hours; the rc-cut GATE)

Every matrix cell: all three k8s profiles, docker, local, both data paths each,
UI-driven. This is the gate for cutting a release candidate -- nothing ships
`rc.N` until T2 is green. Run the three k8s profiles in turn (leaving `scale`
up as the T0 rig), then T1a and T1b:

    python3 scripts/dfe-ops cycle --mode slim   --kubeconfig .tmp/target.kubeconfig --env-file bootstrap/.env
    python3 scripts/dfe-ops cycle --mode single --kubeconfig .tmp/target.kubeconfig --env-file bootstrap/.env
    python3 scripts/dfe-ops stack-deploy --mode scale --kubeconfig .tmp/target.kubeconfig --env-file bootstrap/.env

> **`dfe-ops verify --tier {iterative,local,docker,k8s,full}`** is the intended
> single-entry wrapper over the commands above. It lands once every tier has a
> real backing to call (the local + docker harnesses); until then, run the
> concrete command shown per tier.

## <a name="driving"></a>Driving the product -- the hard rule

Every UI surface is proven with Playwright, in ignore-SSL-errors mode (DFE
serves edge TLS from a per-rebuild self-signed cluster CA), and analysed from a
HUMAN USER's perspective -- layout, legibility, flow, does it look right AND
work right -- never "the element rendered" or a logical pass. Every data path is
proven with real events streamed end to end into ClickHouse and the rows read
back. Clean up browsers and stray headless chromium after every run.

## <a name="egress"></a>Proving egress, not rendering it

A deployment pointed at an external ClickHouse, Kafka, IdP, secret backend or OTel
destination has an egress allow derived from that declaration, and a rendered
NetworkPolicy proves nothing about reachability -- a default-deny drop looks
exactly like a slow endpoint. `scripts/probe_egress.py` execs a TCP probe in the
pod the policy selects and reports OK, REFUSED (packets arrived, egress is not the
fault), TIMEOUT (what a drop looks like) or NO-PROBE. Run it after any deploy that
declares an external service.

## <a name="local-dev"></a>Local-dev docs

Each interdependent repo carries a local-dev `.md` (linked from its
CONTRIBUTING) on running it locally and against the other repos: dfe-engine
`docs/LOCAL-DEV.md`, dfe-ui, dfe-hyperdx. The T1a target is exactly that path.
