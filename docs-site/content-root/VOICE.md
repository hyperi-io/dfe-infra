# Voice

How the site's pages read, one rule a line in the form "say X, not Y". Add a line when a page gets a correction worth keeping. `scripts/aggregate.py` leaves this file out of every build, so it never publishes.

- Say `--` or `-`, not an em-dash or an en-dash.
- Say `->`, not an arrow glyph.
- Say `'` and `"`, not curly quotes.
- Say `...`, not the one-character ellipsis.
- Say two sentences, or join them with and, but or because, not a semicolon.
- Say organisation, optimised, behaviour, licence (the noun), not organization, optimized, behavior, license.
- Say Elastic, not Elasticsearch.
- Say OpenTofu, not Terraform.
- Say what DFE does, not how another pipeline product does it.
- Say the fact, not a hedge ("should", "may", "is expected to") in front of it.
- Say what the reader does next, not what the page is about to do ("This page covers...").
- Say it once, not a second sentence that restates the first.
- Say use, not leverage or utilise.
- Say what it withstands, not robust.
- Say what it covers, not comprehensive or end-to-end.
- Say the cost item (a NAT gateway, a broker hour), not the amount.

## Never ship

Notes to self ("To be proven end to end", "TODO", "not written yet"), lines about the page itself ("Each card links to the guide that owns it"), and process: rounds, release candidates, workstreams, sign-off passes, test cycles, who decided. A dated incident ("broken on 2026-07-14") belongs in the commit history, not on a page.
