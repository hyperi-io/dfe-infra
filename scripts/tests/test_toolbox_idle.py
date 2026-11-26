#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_toolbox_idle.py
#  Purpose:      Hold the toolbox pod's idle scale-down to its rule: what it
#                renders per key, what it decides, and what it tells a terminal.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The toolbox pod scales to zero when idle, never mid-session.

Each layer runs the file the chart ships (helm/charts/dfe-toolbox/files):

- decide.sh, the one rule, as a table of epoch inputs and the answer.
- probe.sh and warn.sh against a scratch /proc, so no real terminal is written to.
- idle-check.sh against a kubectl that only answers the two API calls it makes.
- the chart renders for each key combination, and the activity hook in a real
  dash and bash on a terminal.

    python3 -m pytest scripts/tests/test_toolbox_idle.py

Needs `bash`, `dash` and `helm` on PATH.
"""

import os
import pty
import re
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "dfe-toolbox"
FILES = CHART / "files"
NOW = 1_800_000_000


def run_script(
    script: str, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(FILES / script), *args],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )


# --- decide.sh: the table ---------------------------------------------------------


def decide(**env: object) -> str:
    base = {"NOW": NOW, "START": NOW - 100, "BUSY": 0, "WARN": 300}
    merged = {key: str(value) for key, value in {**base, **env}.items()}
    result = run_script("decide.sh", merged)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


DECIDE_CASES = [
    pytest.param(
        {"IDLE": 1800, "ACTIVITY": NOW - 60},
        f"keep idle 1740 i{NOW - 60}",
        id="active-shell-keeps-the-pod",
    ),
    pytest.param(
        {"IDLE": 1800, "START": NOW - 9000, "ACTIVITY": NOW - 1900},
        f"scale idle 0 i{NOW - 1900}",
        id="idle-past-the-limit-scales",
    ),
    pytest.param(
        {"IDLE": 1800, "START": NOW - 1800, "ACTIVITY": NOW - 1800},
        f"scale idle 0 i{NOW - 1800}",
        id="idle-exactly-at-the-limit-scales",
    ),
    pytest.param(
        {"IDLE": 1800, "START": NOW - 9000, "ACTIVITY": NOW - 1600},
        f"warn idle 200 i{NOW - 1600}",
        id="inside-the-warn-window-warns",
    ),
    pytest.param(
        {
            "IDLE": 1800,
            "START": NOW - 9000,
            "ACTIVITY": NOW - 1600,
            "WARNED": f"i{NOW - 1600}",
        },
        f"keep idle 200 i{NOW - 1600}",
        id="already-warned-for-this-idle-spell-stays-quiet",
    ),
    pytest.param(
        {
            "IDLE": 1800,
            "START": NOW - 9000,
            "ACTIVITY": NOW - 10,
            "WARNED": f"i{NOW - 1600}",
        },
        f"keep idle 1790 i{NOW - 10}",
        id="a-command-after-the-warning-resets-the-clock",
    ),
    pytest.param(
        {"IDLE": 1800, "START": NOW - 100, "ACTIVITY": ""},
        f"keep idle 1700 i{NOW - 100}",
        id="no-activity-file-counts-from-the-pod-start",
    ),
    pytest.param(
        {"IDLE": 1800, "START": NOW - 2000, "ACTIVITY": ""},
        f"scale idle 0 i{NOW - 2000}",
        id="an-untouched-pod-still-scales",
    ),
    pytest.param(
        {"IDLE": 1800, "START": NOW - 9000, "ACTIVITY": NOW - 5000, "BUSY": 1},
        f"keep idle 1800 i{NOW}",
        id="a-running-command-holds-the-pod",
    ),
    pytest.param(
        {"IDLE": 1800, "START": NOW - 9000, "ACTIVITY": NOW + 500},
        f"keep idle 1800 i{NOW}",
        id="a-pod-clock-ahead-is-not-negative-idle",
    ),
    pytest.param(
        {"IDLE": 1800, "START": NOW - 5000, "ACTIVITY": NOW - 9000},
        f"scale idle 0 i{NOW - 5000}",
        id="activity-older-than-the-start-is-ignored",
    ),
    pytest.param(
        {"TTL": 3600, "START": NOW - 3700, "IDLE": 1800, "ACTIVITY": NOW - 1},
        f"scale ttl 0 t{NOW - 3700}",
        id="the-ceiling-scales-an-active-pod",
    ),
    pytest.param(
        {
            "TTL": 3600,
            "START": NOW - 3700,
            "IDLE": 1800,
            "ACTIVITY": NOW - 1,
            "BUSY": 1,
        },
        f"scale ttl 0 t{NOW - 3700}",
        id="the-ceiling-scales-a-busy-pod",
    ),
    pytest.param(
        {"TTL": 3600, "START": NOW - 3400, "IDLE": 1800, "ACTIVITY": NOW - 1},
        f"warn ttl 200 t{NOW - 3400}",
        id="the-ceiling-warns-ahead-of-itself",
    ),
    pytest.param(
        {"TTL": 3600, "START": NOW - 100, "IDLE": 1800, "ACTIVITY": NOW - 1},
        f"keep idle 1799 i{NOW - 1}",
        id="the-nearer-limit-is-the-one-reported",
    ),
    pytest.param(
        {"TTL": 3600, "START": NOW - 100},
        f"keep ttl 3500 t{NOW - 100}",
        id="ceiling-only",
    ),
    pytest.param({"START": NOW - 100}, "keep none 0 -", id="no-limit-set"),
    pytest.param(
        {"IDLE": 120, "START": NOW - 9000, "ACTIVITY": NOW - 10, "WARN": 300},
        f"warn idle 110 i{NOW - 10}",
        id="a-window-wider-than-the-limit-warns-at-once",
    ),
    pytest.param(
        {"IDLE": 1800, "START": NOW - 9000, "ACTIVITY": NOW - 1790, "WARN": 0},
        f"keep idle 10 i{NOW - 1790}",
        id="a-zero-warn-never-warns",
    ),
    pytest.param(
        {"IDLE": "01800", "START": NOW - 9000, "ACTIVITY": NOW - 1600, "WARN": "0300"},
        f"warn idle 200 i{NOW - 1600}",
        id="leading-zeros-read-as-base-10",
    ),
    pytest.param(
        {"IDLE": 1800, "START": NOW - 9000, "ACTIVITY": NOW - 1600, "WARN": "08"},
        f"keep idle 200 i{NOW - 1600}",
        id="a-leading-zero-eight-is-not-an-octal-error",
    ),
]


@pytest.mark.requires_tool("bash")
@pytest.mark.parametrize(("env", "expected"), DECIDE_CASES)
def test_decide_table(env: dict[str, object], expected: str) -> None:
    assert decide(**env) == expected


@pytest.mark.requires_tool("bash")
@pytest.mark.parametrize(
    "bad",
    [
        {"IDLE": "30m"},
        {"ACTIVITY": "yesterday"},
        {"TTL": "-5"},
        {"BUSY": "yes"},
        {"NOW": ""},
    ],
)
def test_decide_rejects_a_value_that_is_not_a_number(bad: dict[str, str]) -> None:
    env = {"NOW": str(NOW), "START": str(NOW - 100), "BUSY": "0", "IDLE": "1800"}
    env.update(bad)
    result = run_script("decide.sh", env)
    assert result.returncode == 2, result.stdout
    assert "not a whole number" in result.stderr


# --- probe.sh and warn.sh against a scratch /proc -----------------------------------


class Pod:
    """A scratch /proc and state directory shaped like the toolbox container's."""

    def __init__(self, root: Path) -> None:
        self.proc = root / "proc"
        self.state = root / "state"
        self.proc.mkdir()
        self.state.mkdir()

    def process(self, pid: int, stdin: str, comm: str) -> None:
        (self.proc / str(pid) / "fd").mkdir(parents=True)
        (self.proc / str(pid) / "fd" / "0").symlink_to(stdin)
        (self.proc / str(pid) / "comm").write_text(comm + "\n", encoding="utf-8")

    @property
    def env(self) -> dict[str, str]:
        return {
            "DFE_TOOLBOX_PROC": str(self.proc),
            "DFE_TOOLBOX_STATE": str(self.state),
        }


@pytest.fixture
def pod(tmp_path: Path) -> Pod:
    return Pod(tmp_path)


def probed(pod: Pod) -> dict[str, str]:
    result = run_script("probe.sh", pod.env)
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


@pytest.mark.requires_tool("bash")
@pytest.mark.parametrize(
    ("processes", "busy"),
    [
        pytest.param([(1, "/dev/null", "sleep")], "0", id="no-terminal"),
        pytest.param(
            [(1, "/dev/null", "sleep"), (7, "/dev/pts/0", "bash")], "0", id="idle-bash"
        ),
        pytest.param([(7, "/dev/pts/0", "dash")], "0", id="idle-dash"),
        pytest.param([(7, "/dev/pts/0", "sh")], "0", id="idle-sh"),
        pytest.param(
            [(7, "/dev/pts/0", "bash"), (9, "/dev/pts/0", "clickhouse-clie")],
            "1",
            id="a-query-on-the-terminal",
        ),
        pytest.param(
            [(7, "/dev/pts/0", "bash"), (9, "/dev/pts/1", "kubectl")],
            "1",
            id="a-log-tail-on-a-second-terminal",
        ),
        pytest.param([(9, "pipe:[1234]", "kubectl")], "0", id="a-pipe-is-no-terminal"),
    ],
)
def test_probe_reports_busy_for_a_command_holding_a_terminal(
    pod: Pod, processes: list[tuple[int, str, str]], busy: str
) -> None:
    for pid, stdin, comm in processes:
        pod.process(pid, stdin, comm)
    assert probed(pod)["busy"] == busy


@pytest.mark.requires_tool("bash")
@pytest.mark.parametrize(
    ("written", "activity", "warned"),
    [
        pytest.param((None, None), "", "", id="no-files"),
        pytest.param(
            ("1799999000\n", "i1799999000"), "1799999000", "i1799999000", id="both"
        ),
        pytest.param(("garbage", "t5"), "", "t5", id="a-torn-activity-file-is-none"),
        pytest.param(("12", "x; rm -rf"), "12", "", id="a-bad-token-is-dropped"),
    ],
)
def test_probe_reads_the_state_files(
    pod: Pod, written: tuple[str | None, str | None], activity: str, warned: str
) -> None:
    if written[0] is not None:
        (pod.state / ".dfe-toolbox-activity").write_text(written[0], encoding="utf-8")
    if written[1] is not None:
        (pod.state / ".dfe-toolbox-warned").write_text(written[1], encoding="utf-8")
    seen = probed(pod)
    assert (seen["activity"], seen["warned"]) == (activity, warned)


def read_notice(fd: int) -> str:
    """Read from a terminal until the notice's closing line break arrives."""
    deadline = time.monotonic() + 5
    seen = b""
    while time.monotonic() < deadline and not seen.endswith(b"\r\n"):
        seen += os.read(fd, 4096)
    return seen.decode(errors="replace")


@pytest.mark.requires_tool("bash")
@pytest.mark.parametrize(
    ("reason", "left", "wording"),
    [
        ("idle", "200", "idle and scales to zero in about 4 min. Run any command"),
        ("idle", "60", "in about 1 min."),
        ("ttl", "1500", "time limit and scales to zero in about 25 min. A command"),
    ],
)
def test_warn_writes_the_notice_to_every_open_terminal_and_records_the_token(
    pod: Pod, reason: str, left: str, wording: str
) -> None:
    master, slave = pty.openpty()
    other_master, other_slave = pty.openpty()
    try:
        pod.process(7, os.ttyname(slave), "bash")
        pod.process(8, "/dev/null", "sleep")
        result = run_script("warn.sh", pod.env, left, reason, "i1799999000")
        assert result.returncode == 0, result.stderr
        text = read_notice(master)
        assert "[dfe-toolbox]" in text
        assert wording in text
        assert (pod.state / ".dfe-toolbox-warned").read_text() == "i1799999000"
        os.set_blocking(other_master, False)
        with pytest.raises(BlockingIOError):
            os.read(other_master, 1)
    finally:
        for fd in (master, slave, other_master, other_slave):
            os.close(fd)


@pytest.mark.requires_tool("bash")
def test_warn_with_no_terminal_still_records_the_token(pod: Pod) -> None:
    pod.process(1, "/dev/null", "sleep")
    assert run_script("warn.sh", pod.env, "100", "idle", "i5").returncode == 0
    assert (pod.state / ".dfe-toolbox-warned").read_text() == "i5"


# --- idle-check.sh against a kubectl that answers its two API calls -------------------

KUBECTL = """#!/usr/bin/env bash
# FAKE_ROWS answers `get pods`, FAKE_PROBE answers probe.sh (FAKE_PROBE_FAIL=1 fails
# it), and every call is logged. Nothing else is simulated.
echo "$*" >>"${FAKE_LOG}"
case "$1" in
get) printf '%s' "${FAKE_ROWS}" ;;
exec)
    case "$*" in
    *probe.sh*)
        [[ "${FAKE_PROBE_FAIL}" == 1 ]] && exit 1
        printf '%s\\n' "${FAKE_PROBE}"
        ;;
    esac
    ;;
esac
"""


def started_at(seconds_ago: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


def run_check(
    tmp_path: Path,
    *,
    started: int | None,
    probe: str = "activity=\nbusy=0\nwarned=",
    probe_fails: bool = False,
    idle: str = "",
    warn: str = "300",
    ttl: str = "",
) -> list[str]:
    """Run idle-check.sh, returning the kubectl calls it made."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    kubectl = bindir / "kubectl"
    kubectl.write_text(KUBECTL, encoding="utf-8")
    kubectl.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    rows = "" if started is None else f"dfe-toolbox-abc {started_at(started)}\n"
    result = subprocess.run(
        ["bash", str(FILES / "idle-check.sh")],
        env={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "SCRIPTS": str(FILES),
            "NAMESPACE": "default",
            "DEPLOYMENT": "dfe-toolbox",
            "SELECTOR": "app.kubernetes.io/name=dfe-toolbox",
            "IDLE_SECONDS": idle,
            "IDLE_WARN_SECONDS": warn,
            "TTL_SECONDS": ttl,
            "FAKE_LOG": str(log),
            "FAKE_ROWS": rows,
            "FAKE_PROBE": probe,
            "FAKE_PROBE_FAIL": "1" if probe_fails else "0",
        },
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    return log.read_text(encoding="utf-8").splitlines()


FIELD_MANAGER = "dfe-toolbox-idle"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-platform.yaml"


def scaled(calls: list[str]) -> bool:
    """A patch of the Deployment to zero replicas, under the named manager."""
    return any(
        call.startswith("patch deployment/dfe-toolbox")
        and "--subresource" not in call
        and """-p {"spec":{"replicas":0}}""" in call
        and f"--field-manager={FIELD_MANAGER}" in call
        for call in calls
    )


def test_the_appset_ignores_the_replicas_the_idle_manager_owns() -> None:
    """Argo CD's selfHeal reverts a scale unless it is told whose field to leave."""
    appset = yaml.safe_load(APPSET.read_text(encoding="utf-8"))
    ignored = appset["spec"]["template"]["spec"]["ignoreDifferences"]
    assert ignored == [
        {
            "group": "apps",
            "kind": "Deployment",
            "name": "dfe-toolbox",
            "managedFieldsManagers": [FIELD_MANAGER],
        }
    ]
    script = (FILES / "idle-check.sh").read_text(encoding="utf-8")
    assert f"readonly FIELD_MANAGER={FIELD_MANAGER}\n" in script


def warnings(calls: list[str]) -> list[str]:
    return [call for call in calls if "warn.sh" in call]


def seconds_ago(seconds: int) -> int:
    return int(time.time()) - seconds


@pytest.mark.requires_tool("bash")
def test_check_with_no_running_pod_does_nothing(tmp_path: Path) -> None:
    calls = run_check(tmp_path, started=None, idle="60")
    assert not scaled(calls)
    assert warnings(calls) == []


@pytest.mark.requires_tool("bash")
def test_check_scales_an_untouched_pod_past_the_idle_limit(tmp_path: Path) -> None:
    assert scaled(run_check(tmp_path, started=4000, idle="1800"))


@pytest.mark.requires_tool("bash")
def test_check_leaves_a_pod_with_recent_activity(tmp_path: Path) -> None:
    probe = f"activity={seconds_ago(30)}\nbusy=0\nwarned="
    calls = run_check(tmp_path, started=4000, idle="1800", probe=probe)
    assert not scaled(calls)
    assert warnings(calls) == []


@pytest.mark.requires_tool("bash")
def test_check_leaves_a_pod_running_a_command(tmp_path: Path) -> None:
    probe = f"activity={seconds_ago(5000)}\nbusy=1\nwarned="
    assert not scaled(run_check(tmp_path, started=9000, idle="1800", probe=probe))


@pytest.mark.requires_tool("bash")
def test_check_warns_inside_the_window_with_the_idle_token(tmp_path: Path) -> None:
    idle_since = seconds_ago(1600)
    probe = f"activity={idle_since}\nbusy=0\nwarned="
    calls = run_check(tmp_path, started=9000, idle="1800", probe=probe)
    assert not scaled(calls)
    (warning,) = warnings(calls)
    assert re.search(rf"warn\.sh (19\d|20\d) idle i{idle_since}$", warning), warning


@pytest.mark.requires_tool("bash")
def test_check_does_not_repeat_a_warning(tmp_path: Path) -> None:
    idle_since = seconds_ago(1600)
    probe = f"activity={idle_since}\nbusy=0\nwarned=i{idle_since}"
    calls = run_check(tmp_path, started=9000, idle="1800", probe=probe)
    assert warnings(calls) == []
    assert not scaled(calls)


@pytest.mark.requires_tool("bash")
def test_check_scales_at_the_ceiling_whatever_the_activity(tmp_path: Path) -> None:
    probe = f"activity={seconds_ago(1)}\nbusy=1\nwarned="
    assert scaled(run_check(tmp_path, started=7300, ttl="7200", probe=probe))


@pytest.mark.requires_tool("bash")
def test_check_holds_an_unprobeable_pod_against_the_idle_limit(
    tmp_path: Path,
) -> None:
    calls = run_check(tmp_path, started=9000, idle="1800", probe_fails=True)
    assert not scaled(calls)


@pytest.mark.requires_tool("bash")
def test_check_still_honours_the_ceiling_on_an_unprobeable_pod(
    tmp_path: Path,
) -> None:
    calls = run_check(tmp_path, started=9000, idle="1800", ttl="7200", probe_fails=True)
    assert scaled(calls)


@pytest.mark.requires_tool("bash")
def test_check_reads_a_probe_with_no_busy_line_as_busy(tmp_path: Path) -> None:
    probe = f"activity={seconds_ago(5000)}\nwarned="
    assert not scaled(run_check(tmp_path, started=9000, idle="1800", probe=probe))


# --- the chart renders ------------------------------------------------------------


def render(*sets: str) -> list[dict]:
    cmd = [
        "helm",
        "template",
        "dfe-toolbox",
        str(CHART),
        "--set",
        "global.registry=ghcr.io/example",
        "--set",
        "toolbox.pod.enabled=true",
    ]
    for item in sets:
        flag = "--set-string" if "Seconds=" in item else "--set"
        cmd += [flag, item]
    out = subprocess.run(
        cmd, capture_output=True, text=True, check=False, encoding="utf-8"
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return [doc for doc in yaml.safe_load_all(out.stdout) if doc]


def of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [doc for doc in docs if doc["kind"] == kind]


def cronjob_env(docs: list[dict]) -> dict[str, str]:
    (cronjob,) = of_kind(docs, "CronJob")
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return {e["name"]: e["value"] for e in pod["containers"][0]["env"]}


def toolbox_pod(docs: list[dict]) -> dict:
    (deployment,) = of_kind(docs, "Deployment")
    return deployment["spec"]["template"]["spec"]


@pytest.mark.requires_tool("helm")
def test_both_limits_empty_renders_nothing_extra() -> None:
    docs = render()
    assert of_kind(docs, "CronJob") == []
    assert of_kind(docs, "ConfigMap") == []
    spec = toolbox_pod(docs)
    container = spec["containers"][0]
    assert "env" not in container
    assert [v["name"] for v in spec["volumes"]] == ["work"]
    assert [m["name"] for m in container["volumeMounts"]] == ["work"]


@pytest.mark.requires_tool("helm")
@pytest.mark.parametrize(
    ("sets", "idle", "warn", "ttl"),
    [
        pytest.param(["toolbox.pod.idleSeconds=1800"], "1800", "300", "", id="idle"),
        pytest.param(
            ["toolbox.pod.ttlSeconds=14400"], "", "300", "14400", id="ceiling"
        ),
        pytest.param(
            ["toolbox.pod.idleSeconds=1800", "toolbox.pod.ttlSeconds=14400"],
            "1800",
            "300",
            "14400",
            id="both",
        ),
        pytest.param(
            ["toolbox.pod.idleSeconds=600", "toolbox.pod.idleWarnSeconds=120"],
            "600",
            "120",
            "",
            id="warn-window-set",
        ),
        pytest.param(
            ["toolbox.pod.idleSeconds=600", "toolbox.pod.idleWarnSeconds=0"],
            "600",
            "0",
            "",
            id="warning-off",
        ),
    ],
)
def test_a_limit_renders_the_check_the_hook_and_the_scripts(
    sets: list[str], idle: str, warn: str, ttl: str
) -> None:
    docs = render(*sets)
    env = cronjob_env(docs)
    seen = (env["IDLE_SECONDS"], env["IDLE_WARN_SECONDS"], env["TTL_SECONDS"])
    assert seen == (idle, warn, ttl)

    container = toolbox_pod(docs)["containers"][0]
    hook = {e["name"]: e["value"] for e in container["env"]}
    assert hook["ENV"] == "/opt/dfe-idle/activity.sh"
    assert "PROMPT_COMMAND" in hook
    mounts = {m["name"] for m in container["volumeMounts"]}
    assert mounts == {"work", "tmp", "idle"}

    (config,) = of_kind(docs, "ConfigMap")
    assert set(config["data"]) == {path.name for path in FILES.iterdir()}
    for name, body in config["data"].items():
        assert body == (FILES / name).read_text(encoding="utf-8")


@pytest.mark.requires_tool("helm")
def test_numbers_from_a_values_file_render_as_strings(tmp_path: Path) -> None:
    values = tmp_path / "values.yaml"
    values.write_text(
        "global:\n  registry: ghcr.io/example\n"
        "toolbox:\n  pod:\n    enabled: true\n"
        "    idleSeconds: 1800\n    ttlSeconds: 14400\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        ["helm", "template", "dfe-toolbox", str(CHART), "-f", str(values)],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    env = cronjob_env([doc for doc in yaml.safe_load_all(out.stdout) if doc])
    assert (env["IDLE_SECONDS"], env["TTL_SECONDS"]) == ("1800", "14400")


@pytest.mark.requires_tool("helm")
@pytest.mark.parametrize(
    ("sets", "message"),
    [
        (["toolbox.pod.idleSeconds=30m"], "idleSeconds"),
        (["toolbox.pod.idleSeconds=0"], "idleSeconds"),
        (["toolbox.pod.idleSeconds=-5"], "idleSeconds"),
        (["toolbox.pod.ttlSeconds=1h"], "ttlSeconds"),
        (["toolbox.pod.ttlSeconds=0"], "ttlSeconds"),
        (["toolbox.pod.idleWarnSeconds=soon"], "idleWarnSeconds"),
    ],
)
def test_a_bad_limit_fails_the_render(sets: list[str], message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        render(*sets)


@pytest.mark.requires_tool("helm")
def test_the_check_pod_is_not_the_pod_it_looks_for() -> None:
    docs = render("toolbox.pod.idleSeconds=1800")
    (cronjob,) = of_kind(docs, "CronJob")
    template = cronjob["spec"]["jobTemplate"]["spec"]["template"]
    check_labels = template["metadata"]["labels"]
    key, _, value = cronjob_env(docs)["SELECTOR"].partition("=")
    assert check_labels[key] != value, "the check pod would match its own SELECTOR"

    (policy,) = of_kind(docs, "NetworkPolicy")
    fenced = policy["spec"]["podSelector"]["matchLabels"]
    assert all(check_labels.get(k) != v for k, v in fenced.items()), (
        "the check pod would fall under the toolbox NetworkPolicy"
    )


@pytest.mark.requires_tool("helm")
def test_exec_belongs_to_the_check_serviceaccount_alone() -> None:
    docs = render("toolbox.pod.idleSeconds=1800", "toolbox.pod.kubeApiAccess=true")
    (role,) = of_kind(docs, "Role")
    exec_rules = [rule for rule in role["rules"] if "pods/exec" in rule["resources"]]
    assert [rule["verbs"] for rule in exec_rules] == [["get", "create"]]
    (binding,) = [
        b
        for b in of_kind(docs, "RoleBinding")
        if b["roleRef"]["name"] == role["metadata"]["name"]
    ]
    assert [s["name"] for s in binding["subjects"]] == ["dfe-toolbox-ttl"]
    (viewer,) = of_kind(docs, "ClusterRole")
    assert all("pods/exec" not in rule["resources"] for rule in viewer["rules"])


@pytest.mark.requires_tool("helm")
def test_the_check_may_patch_the_toolbox_deployment_and_nothing_else_of_it() -> None:
    """A scale subresource write leaves no field-manager entry, so it is patch on the Deployment."""
    (role,) = of_kind(render("toolbox.pod.idleSeconds=1800"), "Role")
    deployment_rules = [
        rule
        for rule in role["rules"]
        if any(r.startswith("deployments") for r in rule["resources"])
    ]
    assert deployment_rules == [
        {
            "apiGroups": ["apps"],
            "resources": ["deployments"],
            "resourceNames": ["dfe-toolbox"],
            "verbs": ["get", "patch"],
        }
    ]


@pytest.mark.requires_tool("helm")
@pytest.mark.parametrize("enabled", ["true", "false"])
def test_the_enabled_toggle_changes_the_manifest_so_argo_syncs_it(
    enabled: str,
) -> None:
    """Without it a toggle changes only replicas, which the idle manager's ignore hides."""
    docs = render("toolbox.pod.idleSeconds=1800", f"toolbox.pod.enabled={enabled}")
    (deployment,) = of_kind(docs, "Deployment")
    annotation = deployment["metadata"]["annotations"]["dfe.hyperi.io/toolbox-enabled"]
    assert annotation == enabled


# --- the activity hook in a real shell --------------------------------------------


def wait_for_activity(path: Path, timeout: float = 10) -> str:
    """Return the activity file's value once a prompt has moved it off 0."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = path.read_text(encoding="utf-8").strip()
        if text != "0":
            return text
        time.sleep(0.05)
    raise AssertionError(f"{path} stayed at 0 for {timeout}s")


def attach(argv: list[str], env: dict[str, str], activity: Path) -> tuple[str, str]:
    """Open a shell on a terminal: the activity value at attach, then after one command."""
    activity.write_text("0", encoding="utf-8")
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(argv[0], argv, {**os.environ, **env})  # noqa: S606
    try:
        at_attach = wait_for_activity(activity)
        activity.write_text("0", encoding="utf-8")
        os.write(fd, b"true\n")
        after_command = wait_for_activity(activity)
        os.write(fd, b"exit\n")
    finally:
        os.waitpid(pid, 0)
        os.close(fd)
    return at_attach, after_command


@pytest.mark.requires_tool("helm")
@pytest.mark.requires_tool("dash")
@pytest.mark.requires_tool("bash")
@pytest.mark.parametrize("shell", ["dash", "bash"])
def test_the_hook_marks_activity_at_attach_and_at_each_prompt(
    tmp_path: Path, shell: str
) -> None:
    activity = tmp_path / "activity"
    hook = tmp_path / "activity.sh"
    shipped = "/tmp/.dfe-toolbox-activity"  # noqa: S108
    hook.write_text(
        (FILES / "activity.sh")
        .read_text(encoding="utf-8")
        .replace(shipped, str(activity)),
        encoding="utf-8",
    )
    container = toolbox_pod(render("toolbox.pod.idleSeconds=1800"))["containers"][0]
    rendered = {e["name"]: e["value"] for e in container["env"]}
    env = {
        "ENV": str(hook),
        "PROMPT_COMMAND": rendered["PROMPT_COMMAND"].replace(shipped, str(activity)),
        "PS1": "$ ",
    }
    argv = ["dash", "-i"] if shell == "dash" else ["bash", "--norc", "-i"]
    at_attach, after_command = attach(argv, env, activity)
    assert re.fullmatch(r"\d{10}", at_attach), at_attach
    assert re.fullmatch(r"\d{10}", after_command), after_command
