#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/tests/test_toolbox_inputs.py
#  Purpose:      Unit tests for the dfe-toolbox build-or-skip decision: what the
#                inputs hash covers, how a published tag is judged, and that the
#                workflow builds only what was hashed; no network, no docker.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The registry read is registry_pins' imagetools reader, covered in
test_dfe_stack.py. These hold the script's own half: the hash, the decision
over what the registry returned, and the workflow wiring that keeps the build
from receiving an input the hash never saw.

    python3 scripts/tests/test_toolbox_inputs.py
"""

import argparse
import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import toolbox_inputs as ti  # noqa: E402

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "toolbox-build.yml"
PLATFORMS = ["linux/amd64", "linux/arm64"]
ARGS = ["KUBECTL_VERSION=v1.35.8", "HELM_VERSION=v4.2.4"]
FILES = {"Dockerfile": "FROM debian\n", "entrypoint.sh": "#!/bin/sh\n"}
HASH_A = "a" * 64
HASH_B = "b" * 64


def _context(files: dict[str, str]) -> list[tuple[str, str]]:
    """context_files over a throwaway directory holding these files."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return ti.context_files(root)


def _hash(
    build_args: list[str] | None = None,
    platforms: list[str] | None = None,
    files: dict[str, str] | None = None,
    base_inputs: str = "",
) -> str:
    """inputs_hash over a fixed baseline, with any input replaced."""
    return ti.inputs_hash(
        ARGS if build_args is None else build_args,
        PLATFORMS if platforms is None else platforms,
        _context(FILES if files is None else files),
        base_inputs,
    )


def test_the_hash_ignores_the_order_inputs_arrive_in() -> None:
    reordered = _hash(build_args=list(reversed(ARGS)), platforms=list(reversed(PLATFORMS)))
    expect("reordered build args and platforms hash the same", _hash() == reordered)
    expect("and the hash is a sha256 hex digest", len(_hash()) == 64, _hash())


def test_every_input_moves_the_hash() -> None:
    baseline = _hash()
    variants = {
        "a new build arg value": _hash(build_args=["KUBECTL_VERSION=v1.36.0", ARGS[1]]),
        "an added build arg": _hash(build_args=[*ARGS, "YQ_VERSION=v4.53.6"]),
        "a different platform list": _hash(platforms=["linux/amd64"]),
        "an edited file": _hash(files={**FILES, "Dockerfile": "FROM debian:trixie\n"}),
        "an added file": _hash(files={**FILES, "extra.conf": "x\n"}),
        "a renamed file": _hash(files={"Dockerfile": "FROM debian\n", "init.sh": "#!/bin/sh\n"}),
        "a different base image hash": _hash(base_inputs=HASH_A),
    }
    for what, moved in sorted(variants.items()):
        expect(f"{what} moves the hash", moved != baseline, moved)


def test_a_cloud_image_follows_its_base_image_inputs() -> None:
    expect(
        "two base hashes give two cloud hashes",
        _hash(base_inputs=HASH_A) != _hash(base_inputs=HASH_B),
    )


def test_context_files_name_paths_relative_to_the_context() -> None:
    files = dict(_context({"Dockerfile": "x", "conf/a.yaml": "y"}))
    expect("every file is listed by its POSIX path", sorted(files) == ["Dockerfile", "conf/a.yaml"])


def test_the_committed_base_context_covers_every_file_docker_receives() -> None:
    context = REPO_ROOT / "docker" / "dfe-toolbox" / "base"
    names = [name for name, _ in ti.context_files(context)]
    expect("the Dockerfile is hashed", "Dockerfile" in names, f"{names}")
    expect("and the entrypoint it COPYs", "entrypoint.sh" in names, f"{names}")


def test_decide_builds_unless_the_tag_already_carries_the_hash() -> None:
    cases = {
        "an absent tag builds": (None, ti.Reason.ABSENT),
        "an unannotated tag builds": (ti.Published("sha256:1", None), ti.Reason.UNANNOTATED),
        "a different hash builds": (ti.Published("sha256:1", HASH_B), ti.Reason.CHANGED),
        "the same hash skips": (ti.Published("sha256:1", HASH_A), ti.Reason.MATCH),
    }
    for name, (published, want) in sorted(cases.items()):
        got = ti.decide(published, HASH_A)
        expect(name, got is want, f"got {got}")


def test_only_the_index_annotation_counts() -> None:
    index = {
        "manifests": [{"annotations": {ti.INPUTS_ANNOTATION: HASH_B}}],
        "annotations": {ti.INPUTS_ANNOTATION: HASH_A},
    }
    expect("the index annotation is read", ti.index_inputs(json.dumps(index)) == HASH_A)
    del index["annotations"]
    expect(
        "a per-manifest annotation is not the index's",
        ti.index_inputs(json.dumps(index)) is None,
    )
    non_string = json.dumps({"annotations": {ti.INPUTS_ANNOTATION: 1}})
    expect("a non-string value reads as no hash", ti.index_inputs(non_string) is None)
    try:
        ti.index_inputs("not json")
        raised = False
    except json.JSONDecodeError:
        raised = True
    expect("an unreadable manifest raises rather than reading as unannotated", raised)


def test_the_build_flags_carry_the_hash_as_an_index_annotation() -> None:
    flags = ti.build_flags(ARGS, PLATFORMS, HASH_A)
    expect(
        "the annotation is index-level",
        f"--annotation=index:{ti.INPUTS_ANNOTATION}={HASH_A}" in flags,
        f"{flags}",
    )
    expect("every hashed build arg is passed", all(f"--build-arg={a}" in flags for a in ARGS))
    expect("the hashed platforms are passed", "--platform=linux/amd64,linux/arm64" in flags)
    expect("no flag holds whitespace", all(" " not in flag for flag in flags), f"{flags}")


def _refused(check: Callable[[str], object], text: str) -> bool:
    try:
        check(text)
    except argparse.ArgumentTypeError:
        return True
    return False


def test_malformed_arguments_are_refused() -> None:
    expect("a build arg with no =", _refused(ti.build_arg, "KUBECTL_VERSION"))
    expect("a build arg holding a space", _refused(ti.build_arg, "A=b c"))
    expect("an empty platform list", _refused(ti.platforms, " , "))
    expect("an inputs hash that is not sha256 hex", _refused(ti.sha256_hex, "abc"))
    expect("an empty base hash", _refused(ti.sha256_hex, ""))
    expect("an empty value is a legal build arg", not _refused(ti.build_arg, "A="))


def test_a_build_arg_given_twice_is_named() -> None:
    repeated = ti.duplicate_names(["A=1", "B=2", "A=3"])
    expect("the repeated name is reported", repeated == ["A"], f"{repeated}")


def test_a_skip_is_a_notice_naming_the_digest() -> None:
    line = ti.plan_line("r:t", ti.Reason.MATCH, ti.Published("sha256:d1", HASH_A))
    expect("the skip is a ::notice::", line.startswith("::notice::"), line)
    expect("naming the digest the tag keeps", "sha256:d1" in line, line)
    built = ti.plan_line("r:t", ti.Reason.CHANGED, ti.Published("sha256:d1", HASH_B))
    expect("a rebuild is not reported as a skip", not built.startswith("::notice::"), built)


def test_verify_fails_unless_the_pushed_index_carries_the_hash() -> None:
    cases = {
        "a matching index passes": (ti.Published("d", HASH_A), False),
        "an index that lost the annotation fails": (ti.Published("d", None), True),
        "an index carrying another hash fails": (ti.Published("d", HASH_B), True),
        "a tag gone after the push fails": (None, True),
    }
    for name, (published, fails) in sorted(cases.items()):
        error = ti.pushed_error("r:t", published, HASH_A)
        expect(name, (error is not None) is fails, f"{error}")


def _image_jobs() -> dict[str, dict]:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    return {name: job for name, job in jobs.items() if name != "resolve"}


def _step(job: dict, step_id: str) -> dict:
    return next((s for s in job["steps"] if s.get("id") == step_id), {})


def test_every_image_job_builds_only_what_it_hashed() -> None:
    """A build arg added to the build step alone would never move the hash."""
    jobs = _image_jobs()
    expect("four images are built", sorted(jobs) == ["aws", "azure", "base", "gcp"], f"{jobs}")
    gate = "steps.plan.outputs.build == 'true'"
    for name, job in sorted(jobs.items()):
        plan = _step(job, "plan").get("run", "")
        expect(f"{name} plans with toolbox_inputs.py", "toolbox_inputs.py plan" in plan, plan)
        builds = [s for s in job["steps"] if "docker buildx build" in s.get("run", "")]
        expect(f"{name} has one build step", len(builds) == 1, f"{len(builds)}")
        for build in builds:
            run = build["run"]
            expect(f"{name} builds only when the plan says so", build.get("if") == gate)
            expect(f"{name} builds with the planned flags", '"${flags[@]}"' in run, run)
            for flag in ("--build-arg", "--platform", "--annotation"):
                expect(f"{name}'s build step sets no {flag} of its own", flag not in run, run)
        reads = [s for s in job["steps"] if "toolbox_inputs.py verify" in s.get("run", "")]
        expect(
            f"{name} reads the hash back after every build",
            len(reads) == 1 and reads[0].get("if") == gate,
            f"{reads}",
        )


def test_the_cloud_images_hash_the_base_image_inputs() -> None:
    jobs = _image_jobs()
    expect(
        "base publishes its inputs hash",
        jobs["base"].get("outputs", {}).get("inputs") == "${{ steps.plan.outputs.inputs }}",
    )
    for name in ("aws", "gcp", "azure"):
        plan = _step(jobs[name], "plan").get("run", "")
        expect(
            f"{name} hashes base's inputs",
            '--base-inputs "${{ needs.base.outputs.inputs }}"' in plan,
            plan,
        )
        expect(f"{name} still waits on base", "base" in jobs[name]["needs"])


def test_only_the_digest_pinned_image_warns_when_its_tag_moves() -> None:
    jobs = _image_jobs()
    for name, job in sorted(jobs.items()):
        read_back = next(s for s in job["steps"] if "toolbox_inputs.py verify" in s.get("run", ""))
        pinned = "--pinned-by" in read_back["run"]
        expect(f"{name} names its pins only if it has any", pinned is (name == "base"))


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
