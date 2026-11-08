#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_version_range.py
#  Purpose:      Pin the comparator the constraints DSL is written against --
#                compat-check decides with it and the dead-guard check reports
#                with it, so its edges have to be the same for both.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/version_range.py.

    python3 scripts/tests/test_version_range.py

No third-party deps and no test runner, matching the module it tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

from _expect import expect, standalone, summary

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import version_range


def test_a_leading_v_is_not_part_of_the_version() -> None:
    expect(
        "v26.1.8 and 26.1.8 compare equal",
        version_range.version_tuple("v26.1.8") == version_range.version_tuple("26.1.8"),
        f"{version_range.version_tuple('v26.1.8')}",
    )


def test_four_part_upstream_tags_compare_numerically() -> None:
    """ClickHouse ships 26.3.32.14, which is not semver and not a string sort."""
    expect(
        "26.3.32.14 is above 26.3.9.1",
        version_range.satisfies("26.3.32.14", ">26.3.9.1"),
        "a string compare would put 26.3.32 below 26.3.9",
    )


def test_every_comparator_in_a_spec_must_hold() -> None:
    expect(
        "inside the band",
        version_range.satisfies("26.1.11", ">=26.1.0 <26.2.0"),
    )
    expect(
        "above the upper bound",
        not version_range.satisfies("26.2.3", ">=26.1.0 <26.2.0"),
    )
    expect(
        "below the lower bound",
        not version_range.satisfies("26.0.9", ">=26.1.0 <26.2.0"),
    )


def test_a_bound_with_fewer_parts_than_the_pin_still_compares() -> None:
    """`>=2.0.0` against `17-0.107.0-ferretdb-2.7.0` is the shape in the tree."""
    expect(
        "2.7.0 satisfies >=2.0.0",
        version_range.satisfies("2.7.0", ">=2.0.0"),
    )
    expect(
        "and a shorter pin compares against a longer bound",
        not version_range.satisfies("26.1", ">=26.1.5"),
        "(26,1) < (26,1,5)",
    )


def test_a_malformed_spec_reads_as_unsatisfied() -> None:
    """A guard nobody can parse must report its rule, never quietly hold."""
    expect(
        "a token with no comparator is False",
        not version_range.satisfies("26.1.11", "26.1.11"),
        "otherwise a typo turns a guard into an unconditional pass",
    )


def test_a_compound_tag_parses_per_segment_rather_than_raising() -> None:
    """Each dotted segment contributes its LEADING digits, so a compound backend
    tag yields a tuple that compares but does not mean what it looks like. The
    ferretdb rule uses require-matches for exactly this reason."""
    expect(
        "17-0.107.0-ferretdb-2.7.0 parses without raising",
        version_range.version_tuple("17-0.107.0-ferretdb-2.7.0") == (17, 107, 0, 7, 0),
        f"{version_range.version_tuple('17-0.107.0-ferretdb-2.7.0')}",
    )


def test_an_rc_ranks_below_its_release() -> None:
    """An app rc pin must not satisfy a bound its promoted release would."""
    order = [
        "v1.22.80",
        "v1.22.81-rc.1",
        "v1.22.81-rc.2",
        "v1.22.81-rc.10",
        "v1.22.81",
        "v1.22.82-rc.1",
    ]
    shuffled = [order[i] for i in (4, 2, 5, 0, 3, 1)]
    got = sorted(shuffled, key=version_range.version_key)
    expect("rc.N orders numerically and below its release", got == order, f"{got}")
    for version, spec, want in (
        ("1.22.81-rc.1", "<1.22.81", True),
        ("1.22.81-rc.1", ">=1.22.81", False),
        ("1.22.81-rc.1", "==1.22.81", False),
        ("1.22.81-rc.1", ">=1.22.80 <1.22.81", True),
        ("v1.22.81", "==1.22.81", True),
    ):
        expect(f"{version} {spec} is {want}", version_range.satisfies(version, spec) is want)


def test_only_a_trailing_alpha_beta_or_rc_is_a_prerelease() -> None:
    """Other dashes are upstream build names and keep comparing as they always did."""
    for tag, want in (
        ("v1.22.81-rc.1", True),
        ("2.2.0-beta.3", True),
        ("v1.22.81", False),
        ("17-0.107.0-ferretdb-2.7.0", False),
        ("1.34.1-20251010", False),
    ):
        expect(f"{tag} prerelease is {want}", version_range.is_prerelease(tag) is want)
    expect(
        "a non-rc dash still satisfies the bound it did",
        version_range.satisfies("17-0.107.0-ferretdb-2.7.0", ">=17"),
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
