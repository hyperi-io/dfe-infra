# The attended acceptance cycle

This is the human-in-the-loop pass that signs off a deployment. [testing-cycle.md](testing-cycle.md) is the automated loop (`dfe-ops cycle`) and tests the deploy scripts and charts. This one tests the PRODUCT, by a person using it.

It's the LAST thing we do. The entry condition is a deployment somebody believes is working. Run every step before calling a stack release good. It's manual on purpose. A login that lands on the wrong team, a dashboard that renders for a user with no roles, and an org's rows leaking into another org's view are things an operator sees in seconds, and a test suite catches them when someone knew to write the test.

## How it runs

Two people, one screen each.

- The operator drives the UI. Every step is done through dfe-ui as a logged-in user, not through curl.
- The agent watches the layers underneath (engine, receiver, loader, ClickHouse, dfe-hyperdx, ArgoCD) and says what it sees.
- Fix as you go. A step that fails stops the cycle, the fault gets fixed at source, and the cycle restarts from that step. Reaching the end with a list of known-broken things isn't a pass.

Nothing here is scripted, so log what happened. A skipped step is reported as skipped, never as a pass.

Before starting:

- A deployment the agent has reason to believe works through every stage: the automated cycle passing, every pod healthy, the data path tested.
- The identity fixture. `dfe-ops idp` stands up the tester IdP and its users, and the external-provider steps need identities in your Entra ID tenant. See [`auth-testing.md`](auth-testing.md). Don't build another IdP.
- Deployment specifics (cluster, domain, credentials) come from the deployment's private config, never from this repo.

## Deploy, then log in

1. Deploy. Stand up a full DFE deployment on the target cluster. The agent watches every Application reach Synced/Healthy, every pod go Ready, and the readiness and integration gates pass unaided instead of being waived. Done when those gates pass and the deployment works.
2. Admin login, both ways. The operator logs into the DFE console as an **admin** and exercises the app, including its search views, TWICE: once by local engine login, where the engine is the identity provider and mints the token, then by role-mapped external OIDC (Entra ID), with roles mapped from group membership. Both must give the same result, and a difference between them is the finding. The agent watches the token the engine mints and its claims, the audience check on each app, group-to-role resolution, and the dfe-hyperdx handoff (team assignment and which connection it ends up using).
3. Data analyst login, both ways. The same as step 2, as a **data analyst**. The point is the difference from admin: screens and actions an analyst shouldn't have must be absent, not just unclickable. The agent watches the same chain plus every authorisation decision. A 403 that should have been a 200, and a 200 that should have been a 403, are equally interesting.

## Orgs and isolation

4. Create an org. The agent creates an organisation through the API and watches what governance renders and executes for it: the ClickHouse role, the row policies, and critically whether anything is GRANTED to an identity. A role that exists and is attached to nothing isolates nothing.
5. Send data to that org. The agent posts data through the receiver, tagged to that org, and follows it from receiver to Kafka to loader to ClickHouse, confirming the rows landed with the org's id.
6. Map a group to the org, and test isolation. Together, create the role mapping from an OIDC group to the org id, local engine login first, then Entra ID. The operator then logs in as an org-tied user and looks at the data.

Step 6 is the point of the cycle. It passes when:

- an org-tied user sees ONLY that org's rows, in the UI and in every DFE console search view
- a user tied to two orgs sees exactly those two
- a platform user sees across orgs but NOT the system databases
- a user with no roles is refused everywhere
- a cross-org request is refused outright

Isolation must come from the identity the connection uses, never from a query predicate the UI adds. A filter that a user can edit isn't isolation. If a DFE console search view can be made to return another org's rows by changing the query, the step has failed however tidy the UI looks.

Each step gets what was done, what the agent saw underneath, and pass or fail with the evidence. Failures get fixed at source and the step re-run. The deploy layer isn't the place to hide an app's bug behind a config change.

The cycle passes when every step passes in the same run, on the same deployment, with no step waived.
