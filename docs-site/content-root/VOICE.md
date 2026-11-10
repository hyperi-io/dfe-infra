# Voice

How the site's pages read, one rule a line in the form "say X, not Y". It binds everyone who writes or contributes DFE docs, people and agents alike. Add a line when a page gets a correction worth keeping. `scripts/aggregate.py` leaves this file out of every build, so it never publishes.

- Say `--` or `-`, not an em-dash or an en-dash.
- Say `->`, not an arrow glyph.
- Say `'` and `"`, not curly quotes.
- Say `...`, not the one-character ellipsis.
- Say two sentences, or join them with and, but or because, not a semicolon.
- Say organisation, optimised, behaviour, licence (the noun), not organization, optimized, behavior, license.
- Say Elasticsearch for the search engine and its index, and Elastic for the company and its stack (Beats, Logstash, ingest pipelines). Never ElasticSearch.
- Say OpenTofu, not Terraform.
- Say the transport as "Kafka message bus" or "gRPC, low latency", not "ClickHouse via Kafka or gRPC". Say where it runs as "Kubernetes, Docker and cloud", not "Kubernetes and Docker Compose". Say "open source" in a hero, and keep the licence names for the pages that explain them.
- Say what DFE does differently and why, not a verdict on another product.
- Say a true, checkable fact about a vendor's behaviour by name ("Splunk keeps `_raw` for every event"). A direct criticism of a vendor needs metrics behind it. Without them, soften it a little by grouping the target ("most stacks", "index engines") rather than naming one vendor, but still make the point.
- Say each difference in three beats, what's different, why, and what it does for you, not a pitch. The third beat names the mechanism that does it for you, not a promise.
- Say "Nothing breaks" or "doesn't break", not that DFE "breaks" a convention or an assumption.
- Say "the DFE console" on top-level pages, not HyperDX. Low-level deployment pages may say `dfe-hyperdx`.
- Draw a flow or a sequence left to right (`flowchart LR`). Keep top-down for a layer stack (what runs on what) or a tree, not for a chain of steps.
- Say "fact tables (the observability data)" and "dimension tables (the context)" together, not one pair alone.
- Say what a field does on day one and what promoting it buys, not ClickHouse JSON internals, on top-level pages.
- Say a well-worn idiom that travels across cultures ("your mileage will vary", "nothing breaks"), not a coined metaphor or a local in-joke.
- Say it with some fun: a positive pop-culture or geek reference that lands ("Don't Panic", "Most excellent", a HAL 9000 nod), not corporate blandness, and never a reference used to put something down. The house tone is "Ready Player One" for tech and docs (the book, not the crappy movie), and that's Derek's line. The reference sits on top of the fact, never in place of it, and the page still reads right to someone who doesn't catch it.
- Say what ships, not a maturity state (alpha, beta, coming, roadmap). Leave unshipped things out of the standing pages.
- Say the sales and differentiator points on the landing hero, in the website's words, and get more technical the deeper the page sits. Never sell below the coming-from page.
- Say the fact, not a hedge ("should", "may", "is expected to") in front of it.
- Say what the reader does next, not what the page is about to do ("This page covers...").
- Say it once, not a second sentence that restates the first.
- Say use, not leverage or utilise.
- Say what it withstands, not robust.
- Say what it covers, not comprehensive or end-to-end.
- Say the cost item (a NAT gateway, a broker hour), not the amount.

## Never ship

Notes to self ("To be proven end to end", "TODO", "not written yet"), lines about the page itself ("Each card links to the guide that owns it"), and process: rounds, release candidates, workstreams, sign-off passes, test cycles, who decided. A dated incident ("broken on 2026-07-14") belongs in the commit history, not on a page.
