# The attended acceptance cycle

The human-in-the-loop pass that signs off a deployment. `TESTING-CYCLE.md` is the
automated loop (`dfe-ops cycle`) and proves the machinery; this proves the
PRODUCT, by a person using it.

It is the LAST thing a workstream does, not a checkpoint along the way -- its
entry condition is a deployment somebody already believes is working. Run it end
to end before calling a stack release good. It is deliberately manual:
the things it catches -- a login that lands on the wrong team, a dashboard that
renders for a user with no roles, an org's rows leaking into another org's view
-- are things an operator sees in seconds and a test suite only sees if someone
already knew to write it.

## How it runs

Two people, one screen each.

- **The operator drives the UI.** Every step is done through dfe-ui as a real
  logged-in user, not through curl.
- **The agent watches the layers underneath** -- engine, receiver, loader,
  ClickHouse, HyperDX, ArgoCD -- and says what it sees.
- **Fix as you go.** A step that fails stops the cycle, the fault gets fixed at
  source, and the cycle restarts from that step. Reaching the end with a list of
  known-broken things is not a pass.

Nothing here is scripted, so log what actually happened. A step that was skipped
is reported as skipped, never as passed.

## Before starting

- A deployment the agent has reason to believe is working end to end -- the
  automated cycle green, every pod healthy, the data path proven.
- The identity fixture. Four providers and twelve test users already exist; see
  [`AUTH-TESTING.md`](AUTH-TESTING.md). Do not build new ones.
- Deployment specifics (cluster, domain, credentials) come from the deployment's
  own private config, never from this repo.

## The cycle

### 1. Deploy

Stand up a full DFE deployment on the target cluster.

Agent watches: every Application reaching Synced/Healthy, every pod Ready, and
the readiness plus integration gates passing on their own rather than being
waived.

Done when the agent would bet on it working, not merely on it having deployed.

### 2. Admin login, both ways

Operator logs into dfe-ui as an **admin** and exercises the app, including the
HyperDX view.

Run it TWICE:

1. **Local engine OIDC** -- the engine as its own identity provider.
2. **Role-mapped external OIDC (Entra ID)** -- the same person, same
   expectations, identity from the external provider with roles mapped from
   group membership.

Both must give the same result. A difference between them is the finding.

Agent watches: the token the engine mints and its claims, the audience check on
each app, group-to-role resolution, and the HyperDX handoff -- team assignment
and which connection it ends up using.

### 3. Data analyst login, both ways

The same as step 2, as a **data analyst**.

The point is the difference from admin: screens and actions an analyst should
not have must be absent, not merely unclickable. Run it against local engine
OIDC and then Entra ID, as before.

Agent watches: the same chain, plus every authorisation decision -- a 403 that
should have been a 200, and a 200 that should have been a 403, are equally
interesting.

### 4. Create an org

Agent creates an organisation through the API.

Agent watches: what governance actually renders and executes for it -- the
ClickHouse role, the row policies, and critically whether anything is GRANTED to
an identity. A role that exists and is attached to nothing isolates nothing.

### 5. Send data to that org

Agent posts data through the receiver, tagged to the new org.

Agent watches: receiver to Kafka to loader to ClickHouse, and confirms the rows
landed carrying the org's id.

### 6. Map a group to the org, and prove isolation

Together, create the role mapping from an OIDC group to the org id -- local
engine OIDC first, then Entra ID.

The operator then logs in as an org-tied user and looks at the data.

This is the step the whole cycle exists for. It passes when:

- an org-tied user sees ONLY that org's rows, in the UI and in HyperDX
- a user tied to two orgs sees exactly those two
- a platform user sees across orgs but NOT the system databases
- a user with no roles is refused everywhere
- a cross-org request is refused outright

Isolation must come from the identity the connection uses, never from a query
predicate the UI adds. A filter that a user can edit is not isolation. If
HyperDX can be made to return another org's rows by changing the query, the step
has failed however tidy the UI looks.

## Recording the result

Each step gets: what was done, what the agent saw underneath, and pass or fail
with the evidence. Failures get fixed at source and the step re-run -- the deploy
layer is not the place to paper over an app's bug.

The cycle has passed only when every step passed in the same run, on the same
deployment, with no step waived.
