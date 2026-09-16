<!--
Project:   DFE (Data Fusion Engine) - product suite
File:      shapes/README.md
Purpose:   What compute-shapes.yaml is, the rules it is written under, and how
           to add a cloud key or a use case.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# compute-shapes.yaml -- which shape each workload asks for

`compute-shapes.yaml` declares, per cloud and per use case, the instance family
and modifiers, the architecture, the generation and price policies, the floor
size, which API names the type, and the block-storage profile for every volume.

It names no instance type. A type is an ANSWER: the resolver reads this file plus
the cloud's live API and writes `shapes/resolved/<cloud>-<region>.json`, which
is committed so an API change arrives as a reviewed diff. Keyed by region,
because instance-generation availability is not one worldwide -- a resolve
against a region with no captured file refuses by name, naming the `capture
--region` command that fills it in. Karpenter NodePool constraints render from
the same entries, so a new generation arrives by drift rather than by a plan
change.

## The silent caps are empty on purpose

A small volume carries IOPS and bandwidth maxima of its own, and the attached
instance caps every volume again -- an r9g.2xlarge tops out at 375 MB/s and
12,000 IOPS whatever the gp3 says -- and the cloud clamps without a word. The
`*_ceiling` fields hold figures read from the provider at build time, and the
resolver asserts `volume profile <= size-derived ceiling <= instance ceiling`.
They stay empty until that work lands; `scripts/validate_sizing.py` accepts
empty or a number, and nothing else.

## No lists

Read by `scripts/yaml_subset.py`: nested maps and scalars only, no third-party
parser. Lists are maps keyed by name (`volumes:`, `use_cases:`) or
comma-separated scalars (`modifiers`).

## Adding a cloud key

Copy an existing cloud, set `status: stub`, set `arch` (`arm64` on the clouds,
`amd64` on-prem), and give every one of the nine use cases its fields and at
least one volume. A stub may leave families and sizes empty; it may not leave a
key out, which is what makes it a validated stub rather than a placeholder.
Filling it in later is an edit here plus one tofu body per capability module.

## Adding a use case

Add it to `use_cases:` with a sentence saying what the workload is, add an entry
under EVERY cloud, and add the name to `USE_CASES` in
`scripts/validate_sizing.py`. A use case that exists in one cloud only is the
failure the validator is there to catch.

Then run `python3 scripts/validate_sizing.py`.
