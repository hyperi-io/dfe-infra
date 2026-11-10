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


def test_decide_reads_what_the_published_tag_carries() -> None:
    cases = {
        "an absent tag": (None, ti.Reason.ABSENT),
        "an unannotated tag": (ti.Published("sha256:1", None), ti.Reason.UNANNOTATED),
        "a tag carrying another hash": (ti.Published("sha256:1", HASH_B), ti.Reason.CHANGED),
        "a tag carrying this hash": (ti.Published("sha256:1", HASH_A), ti.Reason.MATCH),
    }
    for name, (published, want) in sorted(cases.items()):
        got = ti.decide(published, HASH_A)
        expect(f"{name} reads as {want}", got is want, f"got {got}")


def test_an_absent_tag_builds_and_a_match_skips() -> None:
    flags = ti.build_flags(ARGS)
    built = ti.plan_outputs(ti.Reason.ABSENT, HASH_A, flags)
    skipped = ti.plan_outputs(ti.Reason.MATCH, HASH_A, flags)
    expect("an absent tag builds", built is not None and built["build"] == "true", f"{built}")
    expect("a matching tag skips", skipped is not None and skipped["build"] == "false", f"{skipped}")
    expect(
        "both hand the build the hashed flags",
        built is not None and built["flags"] == " ".join(flags),
        f"{built}",
    )
    expect(
        "and the merge the annotation carrying the hash",
        built is not None and built["annotation"] == ti.index_annotation(HASH_A),
        f"{built}",
    )


def test_a_published_tag_with_changed_inputs_fails_the_plan() -> None:
    """Building would move a published family tag, which never moves."""
    flags = ti.build_flags(ARGS)
    for reason in (ti.Reason.CHANGED, ti.Reason.UNANNOTATED):
        expect(f"{reason} is a refusal", reason in ti.REFUSED)
        expect(
            f"{reason} emits no build output, so no later step can build",
            ti.plan_outputs(reason, HASH_A, flags) is None,
        )
    expect("an absent tag is not refused", ti.Reason.ABSENT not in ti.REFUSED)
    expect("a matching tag is not refused", ti.Reason.MATCH not in ti.REFUSED)


def test_the_refusal_says_what_to_bump() -> None:
    cases = {
        "changed": ti.Published("sha256:d1", HASH_B),
        "unannotated": ti.Published("sha256:d1", None),
    }
    for name, published in sorted(cases.items()):
        reason = ti.decide(published, HASH_A)
        line = ti.plan_line("r:t", reason, published)
        expect(f"the {name} tag is an ::error::", line.startswith("::error::"), line)
        expect(
            f"the {name} tag names the published tag the inputs changed under",
            "the inputs changed under the published tag r:t" in line,
            line,
        )
        expect(f"the {name} tag says to bump the family pin", "toolbox.dfe-toolbox" in line, line)
        expect(f"the {name} tag names the digest it keeps", "sha256:d1" in line, line)


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


def test_the_build_flags_carry_the_build_args_and_the_merge_carries_the_hash() -> None:
    """Each platform builds from the same args, and only the merged index holds the hash."""
    flags = ti.build_flags(ARGS)
    expect(
        "every hashed build arg is passed", flags == [f"--build-arg={a}" for a in ARGS], f"{flags}"
    )
    expect("no flag holds whitespace", all(" " not in flag for flag in flags), f"{flags}")
    expect(
        "the annotation is index-level",
        ti.index_annotation(HASH_A) == f"index:{ti.INPUTS_ANNOTATION}={HASH_A}",
        ti.index_annotation(HASH_A),
    )


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
    built = ti.plan_line("r:t", ti.Reason.ABSENT, None)
    expect("a new tag is not reported as a skip", not built.startswith("::"), built)


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
    platform_cases = {
        "an index holding exactly the hashed platforms passes": ({*PLATFORMS}, False),
        "an index the merge lost arm64 from fails": ({"linux/amd64"}, True),
        "an index holding a platform nobody hashed fails": ({*PLATFORMS, "linux/s390x"}, True),
        "an index whose platforms could not be read fails": (None, True),
    }
    for name, (found, fails) in sorted(platform_cases.items()):
        error = ti.platform_error("r:t", found, PLATFORMS)
        expect(name, (error is not None) is fails, f"{error}")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


BUILD_JOBS = ("base-build", "cloud-build")
MERGE_JOBS = ("base-merge", "cloud-merge")


def _publishing_jobs() -> dict[str, dict]:
    jobs = _workflow()["jobs"]
    return {name: jobs[name] for name in (*BUILD_JOBS, *MERGE_JOBS)}


def _step(job: dict, step_id: str) -> dict:
    return next((s for s in job["steps"] if s.get("id") == step_id), {})


def _runs(job: dict, text: str) -> list[dict]:
    return [s for s in job["steps"] if text in s.get("run", "")]


def test_every_image_job_builds_only_what_it_hashed() -> None:
    """A build arg added to the build step alone would never move the hash."""
    jobs = _workflow()["jobs"]
    expect(
        "the jobs are resolve, plan, then a build and a merge for base and the clouds",
        sorted(jobs) == sorted(["resolve", "plan", *BUILD_JOBS, *MERGE_JOBS]),
        f"{sorted(jobs)}",
    )
    planned = {
        "base-build": "${{ needs.plan.outputs.base_flags }}",
        "cloud-build": "${{ needs.plan.outputs[format('{0}_flags', matrix.image)] }}",
    }
    for name in BUILD_JOBS:
        job = jobs[name]
        expect(f"{name} takes the planned flags", job["env"].get("PLANNED_FLAGS") == planned[name])
        builds = _runs(job, "docker buildx build")
        expect(f"{name} has one build step", len(builds) == 1, f"{len(builds)}")
        for build in builds:
            run = build["run"]
            expect(f"{name} builds with the planned flags", '"${flags[@]}"' in run, run)
            for flag in ("--build-arg", "--annotation"):
                expect(f"{name}'s build step sets no {flag} of its own", flag not in run, run)
            expect(f"{name} builds the matrix platform alone", '--platform "linux/${ARCH}"' in run)
            expect(f"{name} records provenance", "--provenance=true" in run, run)
            expect(f"{name} records an SBOM", "--sbom=true" in run, run)
            expect(f"{name} pushes by digest, never a tag", "push-by-digest=true" in run, run)
            expect(f"{name} tags nothing", "--tag" not in run, run)


def test_each_platform_builds_on_a_runner_of_its_own_architecture() -> None:
    """arm64 under QEMU on an amd64 runner is what the provenance guard rejects."""
    workflow = _workflow()
    hashed = workflow["env"]["PLATFORMS"].split(",")
    want = [
        {"arch": "amd64", "runner": "ubuntu-26.04"},
        {"arch": "arm64", "runner": "ubuntu-24.04-arm"},
    ]
    for name in BUILD_JOBS:
        job = workflow["jobs"][name]
        targets = job["strategy"]["matrix"]["target"]
        expect(f"{name} builds on one native runner per arch", targets == want, f"{targets}")
        archs = sorted(f"linux/{target['arch']}" for target in targets)
        expect(f"{name} builds every hashed platform", archs == sorted(hashed), f"{archs}")
        expect(f"{name} runs on the matrix runner", job["runs-on"] == "${{ matrix.target.runner }}")
        build = _runs(job, "docker buildx build")[0]["run"]
        guard = build.index("{{.Server.Arch}}") < build.index("docker buildx build")
        expect(f"{name} refuses a runner of another arch before it builds", guard, build)
    text = WORKFLOW.read_text(encoding="utf-8")
    expect("nothing sets up QEMU", "setup-qemu-action" not in text and "binfmt" not in text)


def test_the_merge_joins_the_digests_under_the_tag_with_the_hash() -> None:
    jobs = _workflow()["jobs"]
    builds = {"base-merge": "base-build", "cloud-merge": "cloud-build"}
    for name in MERGE_JOBS:
        job = jobs[name]
        expect(f"{name} waits on its builds", builds[name] in job["needs"], f"{job['needs']}")
        creates = _runs(job, "docker buildx imagetools create")
        expect(f"{name} joins the digests once", len(creates) == 1, f"{len(creates)}")
        run = creates[0]["run"] if creates else ""
        expect(
            f"{name} annotates the index with the planned hash",
            '--annotation "${ANNOTATION}"' in run,
        )
        expect(f"{name} pushes the family tag", '"${IMAGE_PREFIX}/${IMAGE}:${TAG}"' in run, run)
        expect(f"{name} moves latest beside it", '"${IMAGE_PREFIX}/${IMAGE}:latest"' in run, run)
        expect(f"{name} refuses a platform with no digest", "sha256:*" in run and "exit 1" in run)
        expect(
            f"{name} takes the annotation the plan emitted",
            "annotation" in job["env"].get("ANNOTATION", ""),
            f"{job['env']}",
        )
        steps = job["steps"]
        create = next(i for i, s in enumerate(steps) if "imagetools create" in s.get("run", ""))
        reads = [i for i, s in enumerate(steps) if "toolbox_inputs.py verify" in s.get("run", "")]
        expect(f"{name} reads the index back after the push", len(reads) == 1 and reads[0] > create)
        verify = steps[reads[0]]["run"] if reads else ""
        expect(f"{name} checks the hashed platforms", '--platform "${PLATFORMS}"' in verify, verify)
        expect(f"{name} checks the planned hash", '--inputs "${INPUTS}"' in verify, verify)


def test_the_cloud_images_hash_the_base_image_inputs() -> None:
    jobs = _workflow()["jobs"]
    plan = jobs["plan"]
    for name in ("aws", "gcp", "azure"):
        run = _step(plan, name).get("run", "")
        expect(
            f"{name} hashes base's inputs",
            '--base-inputs "${{ steps.base.outputs.inputs }}"' in run,
            run,
        )
    cloud = jobs["cloud-build"]
    expect("the cloud images wait on base's merged index", "base-merge" in cloud["needs"])
    expect(
        "and build only after it merged or was already published",
        all(
            f'contains(fromJSON(\'["success", "skipped"]\'), needs.{job}.result)' in cloud["if"]
            for job in ("base-build", "base-merge")
        ),
        cloud["if"],
    )
    merge = jobs["cloud-merge"]["if"]
    expect(
        "the cloud merge still runs when base was already published",
        "!cancelled()" in merge and "needs.cloud-build.result == 'success'" in merge,
        merge,
    )


def test_a_refusal_fails_the_job_before_any_login() -> None:
    """The plan exits 1 on a refusal, so it has to run before the credential does."""
    jobs = _workflow()["jobs"]
    plan = jobs["plan"]
    uses = [s.get("uses", "") for s in plan["steps"]]
    expect("the plan never logs in", not any("login-action" in u for u in uses), f"{uses}")
    expect("the plan asks for no package scope", "permissions" not in plan)
    for name, job in sorted(_publishing_jobs().items()):
        expect(f"{name} runs after the plan", "plan" in job["needs"], f"{job['needs']}")
        logins = [s for s in job["steps"] if "docker/login-action" in s.get("uses", "")]
        expect(f"{name} logs in once", len(logins) == 1, f"{logins}")
    for name in BUILD_JOBS:
        setups = [s for s in jobs[name]["steps"] if "setup-buildx-action" in s.get("uses", "")]
        expect(f"{name} sets up buildx once", len(setups) == 1, f"{setups}")
    expect(
        "base builds only when its plan says so",
        "needs.plan.outputs.base_build == 'true'" in jobs["base-build"]["if"],
        jobs["base-build"]["if"],
    )
    expect(
        "the clouds build only those their plans said to",
        jobs["cloud-build"]["strategy"]["matrix"]["image"]
        == "${{ fromJSON(needs.plan.outputs.clouds) }}",
    )


def test_no_published_tag_is_overwritten_with_a_warning() -> None:
    """A move under a published tag is a failure now, not a warning to re-pin."""
    text = WORKFLOW.read_text(encoding="utf-8")
    script = (REPO_ROOT / "scripts" / "toolbox_inputs.py").read_text(encoding="utf-8")
    expect("the workflow names no pins to move", "--pinned-by" not in text)
    expect("the script takes no --pinned-by", "--pinned-by" not in script)
    expect("nothing warns that a tag moved", "::warning::" not in text + script)


def test_a_pull_request_plans_every_image_and_pushes_nothing() -> None:
    """Changed inputs without a family-tag bump fail on the pull request, anonymously."""
    workflow = _workflow()
    triggers = workflow.get("on") or workflow.get(True)
    expect(
        "pull requests run on the same paths as main",
        triggers["pull_request"]["paths"] == triggers["push"]["paths"],
        f"{triggers}",
    )
    expect(
        "the workflow token reads only, unless a build job asks",
        workflow["permissions"] == {"contents": "read"},
        f"{workflow['permissions']}",
    )
    job = workflow["jobs"]["plan"]
    expect("the plan runs on pull requests too", "github.event_name" not in job["if"], job["if"])
    runs = [s.get("run", "") for s in job["steps"]]
    plans = [r for r in runs if "toolbox_inputs.py plan" in r]
    expect("it plans all four images", len(plans) == 4, f"{len(plans)}")
    for image in ("base", "aws", "gcp", "azure"):
        expect(f"including dfe-toolbox-{image}", any(f"dfe-toolbox-{image}:" in r for r in plans))
    expect("each over the hashed platforms", all('--platform "${PLATFORMS}"' in r for r in plans))
    uses = [s.get("uses", "") for s in job["steps"]]
    expect("it never sets up buildx", not any("setup-buildx" in u for u in uses), f"{uses}")
    expect("it never builds or pushes", not any("buildx build" in r or "--push" in r for r in runs))
    for name, publishing in sorted(_publishing_jobs().items()):
        expect(
            f"{name} cannot run on a pull request",
            "github.event_name != 'pull_request'" in publishing.get("if", ""),
            f"{publishing.get('if')}",
        )
        expect(
            f"{name} alone carries packages: write",
            publishing.get("permissions", {}).get("packages") == "write",
            f"{publishing.get('permissions')}",
        )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
