#  Project:      dfe-infra
#  File:         scripts/tests/test_edge_acceptance.py
#  Purpose:      Prove the edge acceptance suite and its cycle stage: the send
#                address it needs and refuses to run without, the deploy-repo
#                allow list it writes and restores, the public endpoint it waits
#                for, the send it expects accepted and the in-cluster send it
#                expects refused, the row it finds in main, and the HyperDX
#                search it runs with an engine-minted token.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The edge suite, without a cloud.

The owner's definition of working is a send from outside the cloud, from an
allow-listed address, that lands in `main` and is found through the console and
HyperDX, while a send from an address NOT on the list is refused. Each half is a
decision this file can make without a load balancer: which Service is the door,
when it is ready, what counts as accepted, what counts as refused and why the
refusal proves the list rather than a dead door.

    python3 -m pytest scripts/tests/test_edge_acceptance.py -q
"""

import base64
import importlib.machinery
import importlib.util
import json
import socket
import sys
import threading
import types
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_edge", str(SCRIPTS / "dfe-ops"))
_spec = importlib.util.spec_from_loader("dfeops_edge", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_edge"] = dfeops
_loader.exec_module(dfeops)

from acceptance.edge import run as edge_run  # noqa: E402
from acceptance.edge import steps as edge  # noqa: E402
from acceptance.onboarding import wizard  # noqa: E402
from acceptance.source import steps as source_steps  # noqa: E402

# Documentation ranges only: this repository is public.
SENDER = "203.0.113.7/32"
NAT = ("198.51.100.1/32", "198.51.100.2/32")
EGRESS = ",".join(NAT)
LB_HOST = "receiver-lb.example"
GATEWAY = "gateway-lb.example"


def _yaml(text: str) -> object:
    return YAML(typ="safe").load(text)


# --- the suite and the cycle stage ---------------------------------------------------


def test_edge_is_a_suite_of_its_own_and_never_part_of_all() -> None:
    """`all` runs on every lane, and only a cloud lane has a load balancer to send to."""
    assert dfeops.EDGE_SUITE in dfeops.SUITES
    assert dfeops.EDGE_SUITE in dfeops.BROWSER_SUITES
    assert dfeops.suite_steps(dfeops.EDGE_SUITE) == (dfeops.EDGE_SUITE,)
    assert dfeops.EDGE_SUITE not in dfeops.suite_steps("all")
    assert dfeops.EDGE_RUNNER == SCRIPTS / "acceptance" / "edge" / "run.py"
    assert dfeops.EDGE_RUNNER.is_file()


def _cycle(monkeypatch: pytest.MonkeyPatch, *argv: str) -> tuple[int, list[list[str]]]:
    ran: list[list[str]] = []
    monkeypatch.setattr(dfeops, "_resolve_stack", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_run_streaming", lambda cmd, env=None: ran.append(cmd) or 0)
    monkeypatch.setenv("DFE_ENGINE_REPO", "/nonexistent/dfe-engine")
    args = dfeops.build_parser().parse_args(
        ["cycle", "--skip-capacity-check", "--skip-preflight", "--stack", "2.2.0-rc.99", *argv]
    )
    return dfeops.cmd_cycle(args), ran


def test_expected_fail_a_cycle_asking_for_edge_without_the_send_address_refuses_before_deploying(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Finding the address missing after the deploy costs the deploy, and a skip would read as green."""
    monkeypatch.delenv(dfeops.INGEST_ALLOW_VAR, raising=False)
    rc, ran = _cycle(monkeypatch, "--acceptance-suite", "edge")
    assert rc == 2
    assert ran == []
    assert dfeops.INGEST_ALLOW_VAR in capsys.readouterr().err


def test_expected_fail_a_cycle_given_an_address_that_admits_everyone_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv(dfeops.INGEST_ALLOW_VAR, "0.0.0.0/0")
    rc, ran = _cycle(monkeypatch, "--acceptance-suite", "edge")
    assert (rc, ran) == (2, [])
    assert "every address" in capsys.readouterr().err


def test_a_cycle_reads_the_send_address_from_its_env_file_and_runs_the_edge_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(dfeops.INGEST_ALLOW_VAR, raising=False)
    env_file = tmp_path / "run.env"
    env_file.write_text(f"{dfeops.INGEST_ALLOW_VAR}={SENDER}\n", encoding="utf-8")
    rc, ran = _cycle(monkeypatch, "--acceptance-suite", "edge", "--env-file", str(env_file))
    assert rc == 0
    stage = next(cmd for cmd in ran if cmd[2] == "acceptance")
    assert stage[stage.index("--suite") + 1] == "edge"


def test_a_cycle_on_another_suite_never_asks_for_the_send_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(dfeops.INGEST_ALLOW_VAR, raising=False)
    rc, ran = _cycle(monkeypatch, "--acceptance-suite", "source")
    assert rc == 0
    assert [cmd[2] for cmd in ran] == ["stack-deploy", "verify", "acceptance", "teardown"]


# --- the send address and the cluster's egress ------------------------------------------


@pytest.mark.parametrize(("raw", "want"), [
    ("203.0.113.7", "203.0.113.7/32"),
    ("203.0.113.7/32", "203.0.113.7/32"),
    (" 198.51.100.0/24 ", "198.51.100.0/24"),
    ("198.51.100.9/24", "198.51.100.0/24"),
    ("2001:db8::7", "2001:db8::7/128"),
])
def test_the_send_address_is_one_network(raw: str, want: str) -> None:
    assert dfeops.ingest_allow_cidr({dfeops.INGEST_ALLOW_VAR: raw}) == want


@pytest.mark.parametrize(("raw", "said"), [
    ("", "unset"),
    ("not-an-address", "not a network"),
    ("0.0.0.0/0", "every address"),
    ("::/0", "every address"),
    (f"{SENDER},198.51.100.4/32", "not a network"),
])
def test_expected_fail_a_send_address_that_cannot_prove_the_list_is_refused(raw: str, said: str) -> None:
    with pytest.raises(dfeops.EdgeSuiteError) as refused:
        dfeops.ingest_allow_cidr({dfeops.INGEST_ALLOW_VAR: raw})
    assert said in str(refused.value)
    assert dfeops.INGEST_ALLOW_VAR in str(refused.value)


def test_the_cluster_egress_is_every_nat_address() -> None:
    assert dfeops.egress_cidrs({dfeops.EGRESS_CIDRS_VAR: f" {EGRESS} "}) == list(NAT)


@pytest.mark.parametrize(("raw", "said"), [("", "--from-terraform"), ("198.51.100.1/32,nope", "nope")])
def test_expected_fail_an_unknown_egress_is_refused(raw: str, said: str) -> None:
    """Without it nothing can say the refused send came from an address off the list."""
    with pytest.raises(dfeops.EdgeSuiteError) as refused:
        dfeops.egress_cidrs({dfeops.EGRESS_CIDRS_VAR: raw})
    assert said in str(refused.value)


# --- the allow list in the deploy repo ----------------------------------------------------

DEPLOYED = """\
# The deployer's own overlay.
clickhouse:
  mode: cluster  # kept as written
exposure:
  groups:
    ingest:
      allow:
        - 192.0.2.0/24
      door: loadbalancer
"""


def test_an_absent_overlay_becomes_one_naming_the_sender_alone() -> None:
    text, allow = dfeops.ingest_allow_document(None, SENDER)
    assert allow == [SENDER]
    assert _yaml(text) == {"exposure": {"groups": {"ingest": {"allow": [SENDER]}}}}


def test_the_deployers_own_overlay_keeps_its_comments_keys_and_senders() -> None:
    text, allow = dfeops.ingest_allow_document(DEPLOYED, SENDER)
    assert allow == ["192.0.2.0/24", SENDER]
    doc = _yaml(text)
    assert doc["clickhouse"] == {"mode": "cluster"}
    assert doc["exposure"]["groups"]["ingest"] == {"allow": allow, "door": "loadbalancer"}
    assert "# The deployer's own overlay." in text
    assert "# kept as written" in text


def test_writing_the_sender_twice_names_it_once() -> None:
    once, _ = dfeops.ingest_allow_document(DEPLOYED, SENDER)
    twice, allow = dfeops.ingest_allow_document(once, SENDER)
    assert allow == ["192.0.2.0/24", SENDER]
    assert twice == once


@pytest.mark.parametrize("text", ["exposure:\n", "exposure:\n  groups:\n", "{}\n", "\n"])
def test_an_empty_branch_is_filled_rather_than_refused(text: str) -> None:
    rendered, allow = dfeops.ingest_allow_document(text, SENDER)
    assert allow == [SENDER]
    assert _yaml(rendered)["exposure"]["groups"]["ingest"]["allow"] == [SENDER]


@pytest.mark.parametrize("text", ["- a\n- b\n", "exposure: open\n", "exposure:\n  groups:\n    ingest:\n      allow: everyone\n"])
def test_expected_fail_an_overlay_of_another_shape_is_refused_not_overwritten(text: str) -> None:
    with pytest.raises(dfeops.EdgeSuiteError) as refused:
        dfeops.ingest_allow_document(text, SENDER)
    assert dfeops.INGEST_ALLOW_FILE in str(refused.value)


class DeployRepo:
    """`dfe-ops deploy-values` against one file, answering as the real command prints."""

    def __init__(self, content: str | None, *, get_rc: int = 0, put_rc: int = 0, delete_rc: int = 0) -> None:
        self.content = content
        self.get_rc, self.put_rc, self.delete_rc = get_rc, put_rc, delete_rc
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], env: dict | None = None) -> tuple[int, str, str]:
        self.calls.append(cmd)
        action = next(word for word in cmd if word in ("get", "put", "delete"))
        path = cmd[cmd.index(action) + 1]
        if action == "get":
            if self.get_rc:
                return 1, "", "lookup failed: HTTP 500"
            if self.content is None:
                return 1, f"absent: {path}\n", ""
            return 0, self.content + "\n", ""
        if action == "put":
            if self.put_rc:
                return 1, "", "put failed: HTTP 409 sha does not match"
            self.content = Path(cmd[cmd.index("--file") + 1]).read_text(encoding="utf-8")
            return 0, f"HTTP 200 - put {path}\n", ""
        if self.delete_rc:
            return 1, "", "delete failed: HTTP 403"
        self.content = None
        return 0, f"HTTP 200 - delete {path}\n", ""

    @property
    def actions(self) -> list[str]:
        return [next(w for w in cmd if w in ("get", "put", "delete")) for cmd in self.calls]


DEPLOY_VALUES = ["python3", "dfe-ops", "deploy-values", "--url", "http://localhost:13000"]


def test_an_absent_overlay_is_written_and_restored_by_deleting_it(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = DeployRepo(None)
    monkeypatch.setattr(dfeops, "_run_text", repo)
    allow, original = dfeops.open_ingest(DEPLOY_VALUES, SENDER, {})
    assert (allow, original) == ([SENDER], None)
    assert _yaml(repo.content)["exposure"]["groups"]["ingest"]["allow"] == [SENDER]
    assert all(cmd[: len(DEPLOY_VALUES)] == DEPLOY_VALUES for cmd in repo.calls)

    ok, line = dfeops.restore_ingest(DEPLOY_VALUES, original, {})
    assert ok, line
    assert repo.content is None
    assert repo.actions == ["get", "put", "delete"]


def test_an_existing_overlay_is_merged_and_restored_byte_for_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = DeployRepo(DEPLOYED)
    monkeypatch.setattr(dfeops, "_run_text", repo)
    allow, original = dfeops.open_ingest(DEPLOY_VALUES, SENDER, {})
    assert original == DEPLOYED
    assert allow == ["192.0.2.0/24", SENDER]
    assert SENDER in repo.content

    ok, _ = dfeops.restore_ingest(DEPLOY_VALUES, original, {})
    assert ok
    assert repo.content == DEPLOYED


@pytest.mark.parametrize(("repo", "said"), [
    (DeployRepo(DEPLOYED, get_rc=1), "lookup failed: HTTP 500"),
    (DeployRepo(DEPLOYED, put_rc=1), "sha does not match"),
])
def test_expected_fail_a_deploy_repo_that_refuses_stops_the_suite(
    monkeypatch: pytest.MonkeyPatch, repo: DeployRepo, said: str
) -> None:
    monkeypatch.setattr(dfeops, "_run_text", repo)
    with pytest.raises(dfeops.EdgeSuiteError) as refused:
        dfeops.open_ingest(DEPLOY_VALUES, SENDER, {})
    assert said in str(refused.value)
    assert dfeops.INGEST_ALLOW_FILE in str(refused.value)


def test_expected_fail_a_restore_the_repo_refuses_says_the_door_stays_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dfeops, "_run_text", DeployRepo(None, delete_rc=1))
    ok, line = dfeops.restore_ingest(DEPLOY_VALUES, None, {})
    assert not ok
    assert "could NOT restore" in line
    assert "HTTP 403" in line


def test_deploy_values_reads_its_credential_from_the_cluster_acceptance_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A context or server dropped here reads the Secret, and writes the repo, of another cluster."""
    seen: list[list[str]] = []
    secret = {"data": {"username": base64.b64encode(b"admin").decode(), "password": base64.b64encode(b"pw").decode()}}

    def fake_run(cmd: list[str], **_kwargs: object) -> types.SimpleNamespace:
        seen.append(cmd)
        return types.SimpleNamespace(stdout=json.dumps(secret), returncode=0)

    monkeypatch.setattr(dfeops.subprocess, "run", fake_run)
    args = dfeops.build_parser().parse_args([
        "deploy-values", "get", "infra/common.yaml", "--kubeconfig", "kc", "--context", "ctx", "--server", "https://api.example",
    ])
    assert dfeops._deploy_repo_cred(args) == ("admin", "pw")
    assert seen[0][:7] == ["kubectl", "--kubeconfig", "kc", "--context", "ctx", "--server", "https://api.example"]
    argv = dfeops.deploy_values_argv(13000, "kc", "ctx", "https://api.example")
    parsed = dfeops.build_parser().parse_args([*argv[2:], "get", "infra/common.yaml"])
    assert (parsed.url, parsed.kubeconfig, parsed.context, parsed.server) == (
        "http://localhost:13000", "kc", "ctx", "https://api.example")


# --- where HyperDX is served -----------------------------------------------------------------

ROUTES = {"items": [
    {"spec": {"hostnames": ["hyperdx.other.example"], "rules": [{"backendRefs": [{"name": "dfe-hyperdx"}]}]}},
    {"spec": {"hostnames": ["dfe.run.example"], "rules": [{"backendRefs": [{"name": "dfe-ui"}]}]}},
    {"spec": {"hostnames": ["*.run.example", "search.run.example"],
              "rules": [{"backendRefs": [{"name": "dfe-engine"}]}, {"backendRefs": [{"name": "dfe-hyperdx"}]}]}},
]}


def test_hyperdx_is_the_route_whose_backend_is_the_fork_under_the_consoles_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read off the route, not the hostnames map: a deploy repo can rename the host."""
    monkeypatch.setattr(dfeops, "_kubectl_json", lambda *_a, **_k: (0, ROUTES, ""))
    assert dfeops._hyperdx_url(["kubectl"], "dfe.run.example") == "https://search.run.example"


def test_expected_fail_no_hyperdx_route_is_empty_rather_than_a_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dfeops, "_kubectl_json", lambda *_a, **_k: (1, {}, "Forbidden"))
    assert dfeops._hyperdx_url(["kubectl"], "dfe.run.example") == ""
    monkeypatch.setattr(dfeops, "_kubectl_json", lambda *_a, **_k: (0, ROUTES, ""))
    assert dfeops._hyperdx_url(["kubectl"], "dfe.elsewhere.example") == ""


# --- `dfe-ops acceptance --suite edge` --------------------------------------------------------


def _acceptance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: DeployRepo, *argv: str,
    runner_rc: int = 0, expect: int = 0, settings: dict[str, str] | None = None,
) -> tuple[list[tuple], list[str], dict[str, str]]:
    """Run cmd_acceptance --suite edge with the cluster stubbed; return forwards, the runner's argv and env."""
    forwards: list[tuple] = []
    seen: dict[str, object] = {}

    def fake_run(cmd, *, env=None, **_kwargs):
        if "run.py" in str(cmd[1]):
            seen["argv"] = list(cmd)
            seen["env"] = dict(env or {})
            seen["repo_while_running"] = repo.content
        return types.SimpleNamespace(returncode=runner_rc)

    monkeypatch.setattr(dfeops, "_forward", lambda *args, **_k: forwards.append(args))
    monkeypatch.setattr(dfeops, "_forward_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(dfeops, "_taken_ports", lambda _ports: [])
    monkeypatch.setattr(dfeops, "_secret_value", lambda *_a, **_k: "")
    monkeypatch.setattr(dfeops, "_resolves", lambda _host: True)
    monkeypatch.setattr(dfeops, "admin_credential", lambda *_a, **_k: ("admin", "minted"))
    monkeypatch.setattr(dfeops, "_gateway_address", lambda _kube: GATEWAY)
    monkeypatch.setattr(dfeops, "_kubectl_json", lambda *_a, **_k: (0, ROUTES, ""))
    monkeypatch.setattr(dfeops, "_run_text", repo)
    monkeypatch.setattr(dfeops.subprocess, "run", fake_run)
    for name in (dfeops.NEW_ADMIN_PASSWORD_VAR, "DFE_NAMESPACE", dfeops.EDGE_FENCE_VAR,
                 dfeops.INGEST_ALLOW_VAR, dfeops.EGRESS_CIDRS_VAR, "KUBECONFIG", "DFE_KUBE_CONTEXT",
                 "DFE_KUBE_SERVER"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (settings if settings is not None else
                        {dfeops.INGEST_ALLOW_VAR: SENDER, dfeops.EGRESS_CIDRS_VAR: EGRESS}).items():
        monkeypatch.setenv(name, value)
    args = dfeops.build_parser().parse_args([
        "acceptance", "--repo", "/nonexistent/dfe-engine", "--suite", "edge", "--namespace", "dfe-run",
        "--ui-url", "https://dfe.run.example", "--shots-dir", str(tmp_path / "shots"), *argv,
    ])
    assert dfeops.cmd_acceptance(args) == expect
    return forwards, seen.get("argv", []), seen


def _flag(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, word in enumerate(argv) if word == flag]


def test_the_edge_stage_writes_the_list_hands_the_runner_everything_and_restores_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = DeployRepo(DEPLOYED)
    forwards, argv, seen = _acceptance(monkeypatch, tmp_path, repo, "--insecure")
    assert _flag(argv, "--allow") == ["192.0.2.0/24", SENDER]
    assert _flag(argv, "--egress-cidrs") == [EGRESS]
    assert _flag(argv, "--namespace") == ["dfe-run"]
    assert _flag(argv, "--gateway-address") == [GATEWAY]
    assert _flag(argv, "--hyperdx-url") == ["https://search.run.example"]
    assert _flag(argv, "--kubectl") == ["kubectl"]
    assert "--insecure" in argv
    # The runner sees the list in place, and the deploy repo has its own file back after it.
    assert SENDER in seen["repo_while_running"]
    assert repo.content == DEPLOYED
    git = next(f for f in forwards if f[0] == "git")
    assert git[2:5] == ("forgejo", "svc/dfe-forgejo", 13000)
    # The runner's own parser has to take every flag it is handed.
    parsed = edge_run.build_parser().parse_args(argv[2:])
    assert parsed.allow == ["192.0.2.0/24", SENDER]


def test_keep_leaves_the_door_open_for_a_look(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = DeployRepo(None)
    _acceptance(monkeypatch, tmp_path, repo, "--keep")
    assert repo.actions == ["get", "put"]


def test_expected_fail_a_failed_restore_fails_the_stage_even_after_a_clean_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _acceptance(monkeypatch, tmp_path, DeployRepo(None, delete_rc=1), expect=1)
    assert "could NOT restore" in capsys.readouterr().err


def test_expected_fail_a_failed_runner_still_restores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = DeployRepo(DEPLOYED)
    _acceptance(monkeypatch, tmp_path, repo, runner_rc=1, expect=1)
    assert repo.content == DEPLOYED


@pytest.mark.parametrize(("settings", "said"), [
    ({dfeops.EGRESS_CIDRS_VAR: EGRESS}, dfeops.INGEST_ALLOW_VAR),
    ({dfeops.INGEST_ALLOW_VAR: SENDER}, dfeops.EGRESS_CIDRS_VAR),
])
def test_expected_fail_the_stage_refuses_rather_than_skips_without_its_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture,
    settings: dict[str, str], said: str,
) -> None:
    repo = DeployRepo(DEPLOYED)
    forwards, argv, _ = _acceptance(monkeypatch, tmp_path, repo, expect=1, settings=settings)
    assert (forwards, argv, repo.calls) == ([], [], [])
    assert said in capsys.readouterr().err


def test_expected_fail_a_refused_allow_list_never_starts_the_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _forwards, argv, _ = _acceptance(monkeypatch, tmp_path, DeployRepo(DEPLOYED, put_rc=1), expect=1)
    assert argv == []


# --- the runner's steps: the marker -------------------------------------------------------------


def test_a_run_id_is_one_search_token_and_safe_in_sql() -> None:
    first, second = edge.new_run_id(), edge.new_run_id()
    assert first != second
    for run_id in (first, second):
        assert run_id.startswith("edge")
        assert run_id.isalnum()
        assert run_id.islower()


def test_the_marker_names_the_run_and_carries_nothing_a_source_routes_on() -> None:
    record = edge.marker_record("edgeabc")
    assert record[source_steps.MARKER] == {"run": "edgeabc", "kind": edge.MARKER_KIND}
    assert edge.MARKER_KIND == "edge"
    assert not {"data_stream", "app", "agent", "tags"} & set(record)
    assert json.loads(json.dumps(record)) == record


# --- the public endpoint ------------------------------------------------------------------------


def _service(name: str, *, kind: str = "LoadBalancer", ranges: tuple[str, ...] = (SENDER,),
             ingress: dict | None = None, ports: tuple[tuple[str, int, str], ...] = (("http", 8080, "TCP"),)) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"type": kind, "loadBalancerSourceRanges": list(ranges),
                 "ports": [{"name": n, "port": p, "protocol": proto} for n, p, proto in ports]},
        "status": {"loadBalancer": {"ingress": [ingress] if ingress else []}},
    }


def test_the_door_is_the_tcp_load_balancer_with_its_address_and_http_port() -> None:
    doc = {"items": [
        _service("dfe-receiver-public-udp", ingress={"hostname": "udp-lb.example"}, ports=(("syslog", 514, "UDP"),)),
        _service("dfe-receiver-public", ingress={"hostname": LB_HOST},
                 ports=(("lumberjack", 5044, "TCP"), ("http", 8080, "TCP"))),
    ]}
    endpoint = edge.public_endpoint(doc)
    assert endpoint == edge.Endpoint("dfe-receiver-public", LB_HOST, 8080, (SENDER,))
    assert endpoint.ingest_url == f"http://{LB_HOST}:8080/ingest"
    assert endpoint.liveness_url == f"http://{LB_HOST}:8080/livez"


def test_a_load_balancer_on_an_address_rather_than_a_name_is_reached_by_it() -> None:
    endpoint = edge.public_endpoint({"items": [_service("dfe-receiver-public", ingress={"ip": "2001:db8::5"})]})
    assert endpoint.ingest_url == "http://[2001:db8::5]:8080/ingest"


@pytest.mark.parametrize("doc", [
    {"items": []},
    {"items": [_service("dfe-receiver-public")]},
    {"items": [_service("dfe-receiver-public", kind="ClusterIP", ingress={"ip": "192.0.2.10"})]},
])
def test_expected_fail_no_address_yet_is_no_endpoint(doc: dict) -> None:
    assert edge.public_endpoint(doc) is None


def test_an_endpoint_is_ready_once_it_carries_the_list_and_resolves() -> None:
    endpoint = edge.Endpoint("dfe-receiver-public", LB_HOST, 8080, (SENDER, "192.0.2.0/24"))
    assert edge.endpoint_problem(endpoint, ["192.0.2.0/24", SENDER], lambda _host: True) == ""


def test_expected_fail_a_load_balancer_on_the_old_list_is_not_ready() -> None:
    """Argo applies the new list a sync later; until then the door admits the old senders."""
    endpoint = edge.Endpoint("dfe-receiver-public", LB_HOST, 8080, ("192.0.2.0/24",))
    problem = edge.endpoint_problem(endpoint, ["192.0.2.0/24", SENDER], lambda _host: True)
    assert SENDER in problem
    assert "loadBalancerSourceRanges" in problem


def test_expected_fail_an_address_that_does_not_resolve_is_not_ready() -> None:
    endpoint = edge.Endpoint("dfe-receiver-public", LB_HOST, 8080, (SENDER,))
    assert "does not resolve" in edge.endpoint_problem(endpoint, [SENDER], lambda _host: False)


def test_the_wait_polls_until_the_door_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([
        (0, {"items": []}, ""),
        (0, {"items": [_service("dfe-receiver-public", ranges=("192.0.2.0/24",), ingress={"hostname": LB_HOST})]}, ""),
        (0, {"items": [_service("dfe-receiver-public", ingress={"hostname": LB_HOST})]}, ""),
    ])
    asked: list[tuple] = []

    def fake(kube, *argv):
        asked.append(argv)
        return next(answers)

    monkeypatch.setattr(edge, "kubectl_json", fake)
    monkeypatch.setattr(edge, "resolves", lambda _host: True)
    monkeypatch.setattr(edge.time, "sleep", lambda _s: None)
    endpoint, detail = edge.wait_endpoint(["kubectl"], "dfe-run", [SENDER], 60.0)
    assert endpoint is not None
    assert endpoint.address == LB_HOST
    assert len(asked) == 3
    assert asked[0][:4] == ("-n", "dfe-run", "get", "service")
    assert "dfe-receiver-public" in detail
    assert SENDER in detail


def test_expected_fail_a_door_that_never_opens_names_the_overlay_to_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(edge, "kubectl_json", lambda *_a: (0, {"items": []}, ""))
    endpoint, detail = edge.wait_endpoint(["kubectl"], "dfe-run", [SENDER], 0.0)
    assert endpoint is None
    assert "exposure.groups.ingest" in detail


# --- a real listener for the sends -----------------------------------------------------------------


class Listener:
    """A receiver or a HyperDX on a port the kernel picks, answering from a script.

    A path with a trailing slash gets the 308 to the bare path that HyperDX's
    Next.js frontend answers, before any route sees the request.
    """

    def __init__(self) -> None:
        self.statuses: list[int] = []
        self.bodies: dict[str, object] = {}
        self.seen: list[dict[str, object]] = []
        self.port = 0

    def answer(self, method: str, path: str) -> tuple[int, object]:
        bare = path.split("?")[0]
        if bare != "/" and bare.endswith("/"):
            return 308, bare.rstrip("/")
        status = self.statuses.pop(0) if self.statuses else 200
        return status, self.bodies.get(f"{method} {bare}", {})


class _Handler(BaseHTTPRequestHandler):
    def _serve(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        listener: Listener = self.server.listener  # type: ignore[attr-defined]
        # Lower-cased, because header names are case-insensitive and urllib capitalises them.
        headers = {name.lower(): value for name, value in self.headers.items()}
        listener.seen.append({"method": method, "path": self.path, "headers": headers, "body": body})
        status, payload = listener.answer(method, self.path)
        out = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        if status == 308:
            self.send_header("Location", str(payload))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def log_message(self, *_args: object) -> None:
        """A test run prints its own results; the listener's access log is noise."""


@pytest.fixture
def listener() -> Iterator[Listener]:
    stub = Listener()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.listener = stub  # type: ignore[attr-defined]
    stub.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield stub
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# --- the allow-listed send ------------------------------------------------------------------------


def test_a_2xx_is_the_send_accepted(listener: Listener) -> None:
    listener.statuses = [202]
    record = edge.marker_record("edgeabc")
    accepted, detail = edge.post_marker(f"http://127.0.0.1:{listener.port}/ingest", record, 5.0)
    assert accepted, detail
    assert "202" in detail
    assert json.loads(listener.seen[0]["body"]) == record
    assert listener.seen[0]["headers"]["content-type"] == "application/json"


def test_expected_fail_a_4xx_is_a_rejection_and_never_retried(listener: Listener) -> None:
    listener.statuses = [401]
    accepted, detail = edge.post_marker(f"http://127.0.0.1:{listener.port}/ingest", {}, 5.0, interval=0.0)
    assert not accepted
    assert "401" in detail
    assert len(listener.seen) == 1


def test_a_5xx_while_the_targets_register_is_retried(listener: Listener) -> None:
    listener.statuses = [503, 200]
    accepted, detail = edge.post_marker(f"http://127.0.0.1:{listener.port}/ingest", {}, 5.0, interval=0.0)
    assert accepted, detail
    assert "2 attempt" in detail


def test_expected_fail_a_door_that_never_answers_says_so(closed_port: int) -> None:
    accepted, detail = edge.post_marker(f"http://127.0.0.1:{closed_port}/ingest", {}, 0.0, interval=0.0)
    assert not accepted
    assert "never answered" in detail


# --- the row in main ----------------------------------------------------------------------------


class Store:
    host = "clickhouse.example"
    database = "dfe"

    def __init__(self, found: int) -> None:
        self.found = found
        self.asked: list[str] = []

    def scalar(self, sql: str) -> int:
        self.asked.append(sql)
        return self.found


def test_the_marker_found_in_main_is_the_landing() -> None:
    results: list[wizard.StepResult] = []
    store = Store(1)
    assert edge.record_landed(results, store, "edgeabc", 0.0)
    assert (results[0].slug, results[0].status) == ("landed", "done")
    assert "FROM main" in store.asked[0]
    assert source_steps.marked("edgeabc", edge.MARKER_KIND) in store.asked[0]


@pytest.mark.parametrize(("store", "said"), [(Store(0), "not in dfe.main"), (types.SimpleNamespace(host=""), "no datastore")])
def test_expected_fail_a_missing_row_or_datastore_fails_and_never_skips(store: object, said: str) -> None:
    results: list[wizard.StepResult] = []
    assert not edge.record_landed(results, store, "edgeabc", 0.0)
    assert results[0].status == "failed"
    assert said in results[0].detail


# --- the refused send ---------------------------------------------------------------------------


def test_egress_outside_the_list_is_clear() -> None:
    assert edge.egress_inside_allow(list(NAT), [SENDER, "2001:db8::/32"]) == []


def test_expected_fail_egress_the_list_already_admits_is_named() -> None:
    assert edge.egress_inside_allow(list(NAT), ["198.51.100.0/24"]) == ["198.51.100.1/32 in 198.51.100.0/24",
                                                                        "198.51.100.2/32 in 198.51.100.0/24"]


def test_the_probe_readings_are_one_line_per_target() -> None:
    logs = "noise\ncontrol 0 404\ntarget 28 000\n"
    assert edge.probe_readings(logs) == {"control": (0, "404"), "target": (28, "000")}


ENDPOINT = edge.Endpoint("dfe-receiver-public", LB_HOST, 8080, (SENDER,))


@pytest.mark.parametrize("rc", [7, 28])
def test_no_answer_from_the_door_beside_an_answer_from_the_gateway_is_the_refusal(rc: int) -> None:
    status, detail = edge.refused_outcome({"control": (0, "404"), "target": (rc, "000")}, ENDPOINT, GATEWAY, list(NAT), [SENDER])
    assert status == "done"
    for fact in (LB_HOST, GATEWAY, "404", NAT[0], SENDER):
        assert fact in detail


def test_expected_fail_an_answer_from_inside_the_cluster_means_the_list_is_not_enforced() -> None:
    status, detail = edge.refused_outcome({"control": (0, "404"), "target": (0, "200")}, ENDPOINT, GATEWAY, list(NAT), [SENDER])
    assert status == "failed"
    assert "answered 200" in detail


def test_expected_fail_a_pod_that_reaches_nothing_proves_nothing() -> None:
    """A refusal from a pod with no way out is the egress policy talking, not the list."""
    status, detail = edge.refused_outcome({"control": (28, "000"), "target": (28, "000")}, ENDPOINT, GATEWAY, list(NAT), [SENDER])
    assert status == "failed"
    assert "reached nothing" in detail


def test_expected_fail_a_control_the_gateway_connected_but_never_answered_is_not_called_unreachable() -> None:
    """curl exit 35 is a handshake that failed on a connection the gateway accepted."""
    status, detail = edge.refused_outcome({"control": (35, "000"), "target": (28, "000")}, ENDPOINT, GATEWAY, list(NAT), [SENDER])
    assert status == "failed"
    assert "got no HTTP answer (curl exit 35)" in detail
    assert "reached nothing" not in detail


@pytest.mark.parametrize("readings", [{"control": (0, "404"), "target": (6, "000")}, {"control": (0, "404")}, {}])
def test_expected_fail_anything_but_a_timeout_or_a_refused_connect_is_not_a_refusal(readings: dict) -> None:
    assert edge.refused_outcome(readings, ENDPOINT, GATEWAY, list(NAT), [SENDER])[0] == "failed"


RECEIVER = {
    "spec": {"template": {"spec": {
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001},
        "nodeSelector": {"dfe.hyperi.io/pool": "apps"},
        "tolerations": [{"key": "dfe", "operator": "Exists"}],
        "imagePullSecrets": [{"name": "registry-pull"}],
        "containers": [{"name": "dfe-receiver", "image": "registry.example/dfe-receiver@sha256:" + "a" * 64,
                        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}],
    }}},
}


CONSOLE_URL = "https://dfe.run.example"
CONTROL = edge.Control("https://dfe.run.example:443/", f"dfe.run.example:443:{GATEWAY}:443")


def test_the_control_asks_the_gateway_for_the_consoles_own_name() -> None:
    """Envoy picks a listener by SNI, so the load balancer's own name gets no handshake."""
    assert edge.gateway_control(CONSOLE_URL, GATEWAY) == CONTROL
    assert edge.gateway_control("https://dfe.run.example:8443/x", GATEWAY) == edge.Control(
        "https://dfe.run.example:8443/", f"dfe.run.example:8443:{GATEWAY}:8443")
    assert edge.gateway_control(CONSOLE_URL, "2001:db8::7").connect == "dfe.run.example:443:[2001:db8::7]:443"


@pytest.mark.parametrize(("console", "gateway"), [("", GATEWAY), ("not a url", GATEWAY), (CONSOLE_URL, "")])
def test_expected_fail_no_host_or_no_gateway_makes_no_control(console: str, gateway: str) -> None:
    assert edge.gateway_control(console, gateway) is None


def test_the_probe_dials_the_control_through_the_connect_to_variable() -> None:
    assert '--connect-to "$PROBE_CONTROL_CONNECT" "$PROBE_CONTROL_URL"' in edge.PROBE_SCRIPT


def test_the_probe_pod_runs_the_receivers_own_image_where_the_receiver_can_run() -> None:
    pod = edge.probe_pod("edge-probe-ns", RECEIVER, CONTROL, ENDPOINT.liveness_url)
    spec = pod["spec"]
    container = spec["containers"][0]
    template = RECEIVER["spec"]["template"]["spec"]
    assert container["image"] == template["containers"][0]["image"]
    assert container["securityContext"] == template["containers"][0]["securityContext"]
    assert spec["securityContext"] == template["securityContext"]
    assert spec["nodeSelector"] == template["nodeSelector"]
    assert spec["tolerations"] == template["tolerations"]
    assert spec["imagePullSecrets"] == [{"name": "registry-pull"}]
    assert spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False
    assert spec["activeDeadlineSeconds"] > 0
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env == {"PROBE_CONTROL_URL": CONTROL.url, "PROBE_CONTROL_CONNECT": CONTROL.connect,
                   "PROBE_TARGET_URL": ENDPOINT.liveness_url}
    # The addresses reach the shell as variables, never spliced into the script.
    assert LB_HOST not in container["command"][-1]
    assert GATEWAY not in container["command"][-1]
    assert "dfe.run.example" not in container["command"][-1]
    assert pod["metadata"]["namespace"] == "edge-probe-ns"


class Kube:
    """kubectl for the probe: the receiver, a pull secret, a pod that finishes, its logs."""

    SECRET_VALUE = base64.b64encode(b"registry-token-value").decode()

    def __init__(self, logs: str = "control 0 404\ntarget 28 000\n", phase: str = "Succeeded") -> None:
        self.logs, self.phase = logs, phase
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def __call__(self, kube: list[str], *argv: str, stdin: str = "") -> tuple[int, str, str]:
        self.calls.append((argv, stdin))
        if argv[2:4] == ("get", "deployment"):
            return 0, json.dumps(RECEIVER), ""
        if argv[2:4] == ("get", "secret"):
            return 0, json.dumps({"type": "kubernetes.io/dockerconfigjson", "data": {".dockerconfigjson": self.SECRET_VALUE},
                                  "metadata": {"name": "registry-pull", "annotations": {"x": "y"}}}), ""
        if argv[2:4] == ("get", "pod"):
            return 0, json.dumps({"status": {"phase": self.phase}}), ""
        if argv[2:3] == ("logs",):
            return 0, self.logs, ""
        return 0, "", ""

    def verbs(self) -> list[str]:
        return [" ".join(a for a in argv if not a.startswith("edge")) for argv, _ in self.calls]


def _refused(monkeypatch: pytest.MonkeyPatch, kube: Kube, *, allow: tuple[str, ...] = (SENDER,),
             gateway: str = GATEWAY, console_url: str = CONSOLE_URL) -> list[wizard.StepResult]:
    monkeypatch.setattr(edge, "kubectl", kube)
    monkeypatch.setattr(edge.time, "sleep", lambda _s: None)
    results: list[wizard.StepResult] = []
    edge.record_refused(results, ["kubectl"], "dfe-run", ENDPOINT, gateway, list(allow), list(NAT), "edgeabc",
                        console_url=console_url)
    return results


def test_the_refusal_runs_in_a_namespace_of_its_own_and_takes_it_away(monkeypatch: pytest.MonkeyPatch) -> None:
    kube = Kube()
    results = _refused(monkeypatch, kube)
    assert (results[0].slug, results[0].status) == ("refused", "done"), results[0].detail
    created = [json.loads(stdin) for argv, stdin in kube.calls if argv[:1] == ("create",)]
    assert [doc["kind"] for doc in created] == ["Namespace", "Secret", "Pod"]
    namespace = created[0]["metadata"]["name"]
    assert namespace.startswith("edgeabc")
    # The copy carries the data and nothing of the original's metadata.
    assert created[1]["metadata"] == {"name": "registry-pull", "namespace": namespace}
    assert kube.calls[-1][0][:3] == ("delete", "namespace", namespace)
    # A Secret's data never reaches the step table.
    assert Kube.SECRET_VALUE not in results[0].detail


def test_expected_fail_a_probe_that_never_finished_still_takes_its_namespace_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kube = Kube(phase="Pending")
    monkeypatch.setattr(edge, "PROBE_DEADLINE", 0.0)
    results = _refused(monkeypatch, kube)
    assert results[0].status == "failed"
    assert "Pending" in results[0].detail
    assert kube.calls[-1][0][:2] == ("delete", "namespace")


def test_the_probe_pod_carries_the_control_the_console_names(monkeypatch: pytest.MonkeyPatch) -> None:
    kube = Kube()
    _refused(monkeypatch, kube)
    pod = next(json.loads(stdin) for argv, stdin in kube.calls if argv[:1] == ("create",) and '"Pod"' in stdin)
    env = {item["name"]: item["value"] for item in pod["spec"]["containers"][0]["env"]}
    assert (env["PROBE_CONTROL_URL"], env["PROBE_CONTROL_CONNECT"]) == (CONTROL.url, CONTROL.connect)


@pytest.mark.parametrize(("allow", "gateway", "console_url", "said"), [
    (("198.51.100.0/24",), GATEWAY, CONSOLE_URL, "198.51.100.1/32 in 198.51.100.0/24"),
    ((SENDER,), "", CONSOLE_URL, "no gateway address"),
    ((SENDER,), GATEWAY, "", "no console host"),
])
def test_expected_fail_a_refusal_that_could_prove_nothing_is_never_attempted(
    monkeypatch: pytest.MonkeyPatch, allow: tuple[str, ...], gateway: str, console_url: str, said: str,
) -> None:
    kube = Kube()
    results = _refused(monkeypatch, kube, allow=allow, gateway=gateway, console_url=console_url)
    assert results[0].status == "failed"
    assert said in results[0].detail
    assert kube.calls == []


# --- HyperDX ------------------------------------------------------------------------------------

MAIN = {"id": "64b7f0c2a1b2c3d4e5f60718", "name": "main", "connection": "64b7f0c2a1b2c3d4e5f60719",
        "from": {"databaseName": "dfe", "tableName": "main"},
        "timestampValueExpression": "_timestamp", "implicitColumnExpression": "_json"}


def test_the_main_source_is_found_by_name() -> None:
    assert edge.main_source([{"name": "hunts"}, MAIN]) == MAIN
    assert edge.main_source([{"name": "hunts"}]) is None
    assert edge.main_source({"not": "a list"}) is None


def test_the_search_reads_the_sources_own_table_columns_and_window() -> None:
    sql = edge.hyperdx_count_sql(MAIN, "edgeabc")
    assert "FROM `dfe`.`main`" in sql
    assert "`_timestamp` >= now() - INTERVAL" in sql
    assert "toString(`_json`) ILIKE '%edgeabc%'" in sql
    assert sql.endswith("FORMAT JSONCompact")


@pytest.mark.parametrize(("source", "run_id"), [
    ({**MAIN, "from": {"databaseName": "dfe", "tableName": "main` WHERE 1=1 --"}}, "edgeabc"),
    ({**MAIN, "timestampValueExpression": "toDateTime(x)"}, "edgeabc"),
    (MAIN, "edge'abc"),
])
def test_expected_fail_anything_not_a_plain_name_never_reaches_the_query(source: dict, run_id: str) -> None:
    with pytest.raises(ValueError, match="plain name"):
        edge.hyperdx_count_sql(source, run_id)


class TokenEngine:
    """The engine client's login, minting the one token HyperDX is shown."""

    MINTED = "engine-token"

    def __init__(self) -> None:
        self.token = ""
        self.password = "issued"

    def login(self) -> None:
        self.token = self.MINTED


class ChangingEngine(TokenEngine):
    """An admin still on its issued password: the first login makes the forced change.

    The change ends the session it was made on, so that login's token is dead.
    """

    def __init__(self) -> None:
        super().__init__()
        self.logins = 0

    def login(self) -> None:
        self.logins += 1
        if self.password == "issued":
            self.token, self.password = "token-of-an-ended-session", "chosen"
            return
        self.token = self.MINTED


def test_a_login_that_made_the_forced_change_signs_in_again_for_hyperdx(listener: Listener) -> None:
    listener.bodies = {"GET /api/sources": [MAIN], "POST /api/clickhouse-proxy":{"data": [["1"]]}}
    engine = ChangingEngine()
    results: list[wizard.StepResult] = []
    edge.record_hyperdx(results, f"http://127.0.0.1:{listener.port}", engine, "edgeabc", True, 5.0)
    assert results[0].status == "done", results[0].detail
    assert engine.logins == 2
    assert {seen["headers"]["cookie"] for seen in listener.seen} == {f"dfe_token={TokenEngine.MINTED}"}


def test_hyperdx_finds_the_marker_through_its_own_api(listener: Listener) -> None:
    listener.bodies = {"GET /api/sources": [{"name": "hunts"}, MAIN],
                       "POST /api/clickhouse-proxy":{"data": [["1"]]}}
    results: list[wizard.StepResult] = []
    edge.record_hyperdx(results, f"http://127.0.0.1:{listener.port}", TokenEngine(), "edgeabc", True, 5.0)
    assert (results[0].slug, results[0].status) == ("hyperdx", "done"), results[0].detail
    sources, query = listener.seen
    # The console's iframe credential: the dfe_token cookie, never an Authorization header.
    for seen in (sources, query):
        assert seen["headers"]["cookie"] == "dfe_token=engine-token"
        assert "authorization" not in seen["headers"]
    # The browser client's own path: the frontend 308s the trailing-slash form.
    assert query["path"] == "/api/clickhouse-proxy"
    assert query["headers"]["x-hyperdx-connection-id"] == MAIN["connection"]
    assert "ILIKE '%edgeabc%'" in query["body"]
    assert "engine-token" not in results[0].detail


def test_expected_fail_a_redirect_is_the_finding_and_is_never_followed(
    listener: Listener, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """urllib would carry the token cookie to whatever host a redirect names."""
    monkeypatch.setattr(edge, "CLICKHOUSE_PROXY", "/api/clickhouse-proxy/")
    listener.bodies = {"GET /api/sources": [MAIN]}
    results: list[wizard.StepResult] = []
    edge.record_hyperdx(results, f"http://127.0.0.1:{listener.port}", TokenEngine(), "edgeabc", True, 0.0)
    assert results[0].status == "failed"
    assert "answered 308" in results[0].detail
    assert [seen["path"] for seen in listener.seen] == ["/api/sources", "/api/clickhouse-proxy/"]


def test_expected_fail_no_main_source_names_where_the_fork_seeds_it(listener: Listener) -> None:
    listener.bodies = {"GET /api/sources": [{"name": "hunts"}]}
    results: list[wizard.StepResult] = []
    edge.record_hyperdx(results, f"http://127.0.0.1:{listener.port}", TokenEngine(), "edgeabc", True, 0.0)
    assert results[0].status == "failed"
    assert "no main source" in results[0].detail


def test_expected_fail_a_token_hyperdx_refuses_is_the_finding(listener: Listener) -> None:
    listener.statuses = [401]
    results: list[wizard.StepResult] = []
    edge.record_hyperdx(results, f"http://127.0.0.1:{listener.port}", TokenEngine(), "edgeabc", True, 0.0)
    assert results[0].status == "failed"
    assert "401" in results[0].detail


def test_expected_fail_zero_rows_through_hyperdx_is_a_failure(listener: Listener) -> None:
    listener.bodies = {"GET /api/sources": [MAIN], "POST /api/clickhouse-proxy":{"data": [["0"]]}}
    results: list[wizard.StepResult] = []
    edge.record_hyperdx(results, f"http://127.0.0.1:{listener.port}", TokenEngine(), "edgeabc", True, 0.0)
    assert results[0].status == "failed"
    assert "0 row" in results[0].detail


def test_expected_fail_no_hyperdx_url_fails_rather_than_skips() -> None:
    results: list[wizard.StepResult] = []
    edge.record_hyperdx(results, "", TokenEngine(), "edgeabc", True, 0.0)
    assert results[0].status == "failed"


# --- the console's Observe search with a marker ---------------------------------------------------


class Frame:
    """HyperDX's search frame, already on the source, recording what was typed where.

    The where box opens on *language*, the label its language switch shows; the
    fork opens it on SQL for a profile that never chose.
    """

    url = "https://hyperdx.example/search"

    def __init__(self, language: str = "SQL") -> None:
        self.language = language
        self.visited: list[str] = []
        self.typed: list[tuple[str, str]] = []
        self.pressed: list[str] = []
        self._at = ""

    def _here(self, what: str) -> Frame:
        self.visited.append(what)
        self._at = what
        return self

    def get_by_placeholder(self, name: str) -> Frame:
        return self._here(f"placeholder:{name}")

    def get_by_test_id(self, name: str) -> Frame:
        return self._here(f"testid:{name}")

    def get_by_label(self, name: str) -> Frame:
        return self._here(f"label:{name}")

    def get_by_role(self, role: str, name: str = "", exact: bool = False) -> Frame:
        return self._here(f"{role}:{name}")

    def get_by_text(self, pattern: object) -> Frame:
        return self._here("text")

    @property
    def first(self) -> Frame:
        return self

    def wait_for(self, **_kwargs: object) -> None:
        return None

    def input_value(self, **_kwargs: object) -> str:
        return self.language if self._at.startswith("label:") else "main"

    def fill(self, value: str, **_kwargs: object) -> None:
        self.typed.append((self._at, value))

    def press(self, key: str, **_kwargs: object) -> None:
        self.pressed.append(key)

    def click(self, **_kwargs: object) -> None:
        self.visited.append(f"click:{self._at}")

    def count(self) -> int:
        return 1

    def inner_text(self, **_kwargs: object) -> str:
        return "1 Results"


def test_the_marker_is_searched_in_the_where_box_before_run() -> None:
    frame = Frame()
    assert source_steps.search_results(frame, "main", "edgeabc") == (True, "1 Results")
    assert frame.typed == [(f"testid:{source_steps.WHERE_INPUT}", "edgeabc")]
    assert frame.pressed == ["Escape"]
    assert frame.visited.index(f"testid:{source_steps.WHERE_INPUT}") < frame.visited.index("click:button:Run")


def test_a_box_the_fork_opened_on_sql_goes_to_lucene_before_the_marker_is_typed() -> None:
    """Only the Lucene box carries the search-input test id; the SQL editor has none."""
    frame = Frame(language="SQL")
    assert source_steps.search_results(frame, "main", "edgeabc") == (True, "1 Results")
    switch = frame.visited.index(f"click:label:{source_steps.WHERE_LANGUAGE_LABEL}")
    lucene = frame.visited.index("click:option:Lucene")
    where = frame.visited.index(f"testid:{source_steps.WHERE_INPUT}")
    assert switch < lucene < where


def test_a_box_already_on_lucene_is_left_alone() -> None:
    frame = Frame(language="Lucene")
    assert source_steps.search_results(frame, "main", "edgeabc") == (True, "1 Results")
    assert not any(v.startswith("click:label:") or v == "click:option:Lucene" for v in frame.visited)


class StuckWhereFrame(Frame):
    """A frame whose where box never appears, as in a SQL box nobody switched."""

    def wait_for(self, **_kwargs: object) -> None:
        if self._at == f"testid:{source_steps.WHERE_INPUT}":
            raise TimeoutError('Locator.wait_for: Timeout 30000ms exceeded.\n  - waiting for get_by_test_id("search-input")')


def test_expected_fail_a_search_that_stops_after_the_pick_never_blames_the_picker() -> None:
    picked, results = source_steps.search_results(StuckWhereFrame(language="Lucene"), "main", "edgeabc")
    assert picked
    status, detail = source_steps.observe_outcome("main", Frame.url, "", picked, results, "edgeabc")
    assert status == "failed"
    assert "does not offer" not in detail
    assert detail.startswith("Observe search over main for edgeabc returned the console could not: TimeoutError")
    assert 'get_by_test_id("search-input")' in detail


def test_a_search_with_no_marker_never_touches_the_where_box() -> None:
    frame = Frame()
    source_steps.search_results(frame, "main")
    assert frame.typed == []
    assert not any(v.startswith(("testid:", "label:")) for v in frame.visited)


def test_the_observe_row_names_the_marker_it_searched_for() -> None:
    assert source_steps.observe_outcome("main", Frame.url, "", True, "1 Results", "edgeabc") == (
        "done", "Observe search over main for edgeabc: 1 Results")


# --- the runner: what a failed step stops ---------------------------------------------------------


def _ingest(monkeypatch: pytest.MonkeyPatch, endpoint: object, accepted: bool) -> tuple[list[wizard.StepResult], list[str]]:
    sent: list[str] = []
    monkeypatch.setattr(edge, "wait_endpoint", lambda *_a, **_k: (endpoint, "door detail"))
    monkeypatch.setattr(edge, "post_marker", lambda url, body, deadline, **_k: sent.append(url) or (accepted, "sent detail"))
    results: list[wizard.StepResult] = []
    edge_run.ingest_rows(results, ["kubectl"], "dfe-run", [SENDER], Store(1), "edgeabc")
    return results, sent


def test_the_ingest_rows_run_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    results, sent = _ingest(monkeypatch, ENDPOINT, True)
    assert [(r.slug, r.status) for r in results] == [("public-endpoint", "done"), ("accepted", "done"), ("landed", "done")]
    assert sent == [ENDPOINT.ingest_url]


def test_expected_fail_no_door_fails_every_row_behind_it_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    results, sent = _ingest(monkeypatch, None, True)
    assert [(r.slug, r.status) for r in results] == [("public-endpoint", "failed"), ("accepted", "failed"), ("landed", "failed")]
    assert sent == []


def test_expected_fail_a_rejected_send_fails_the_landing_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    results, _ = _ingest(monkeypatch, ENDPOINT, False)
    assert [(r.slug, r.status) for r in results][1:] == [("accepted", "failed"), ("landed", "failed")]


def test_the_runner_reads_the_datastore_acceptance_names_and_fails_every_row_behind_a_dead_door(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An external ClickHouse is reached over TLS, which only DFE_E2E_CH_SECURE says."""
    seen: dict[str, object] = {}

    def no_door(results, kube, namespace, allow, store, run_id):
        seen["store"] = store
        edge.row(results, "public-endpoint", "failed", "no door")
        return None, False, False

    monkeypatch.setattr(edge_run, "ingest_rows", no_door)
    monkeypatch.setattr(edge, "record_minted_secrets", lambda results, *_a: edge.row(results, "minted-secrets-in-store", "skipped", "stub"))
    for name, value in {"DFE_E2E_ADMIN_PASSWORD": "minted", "DFE_E2E_CH_HOST": "clickhouse.example",
                        "DFE_E2E_CH_PORT": "8443", "DFE_E2E_CH_SECURE": "true"}.items():
        monkeypatch.setenv(name, value)
    args = edge_run.build_parser().parse_args([
        "--ui-url", "https://dfe.example", "--engine-url", "https://dfe.example", "--namespace", "dfe-run",
        "--kubectl", "kubectl", "--allow", SENDER, "--egress-cidrs", EGRESS, "--shots-dir", str(tmp_path),
    ])
    assert edge_run.run(args) == 1
    assert (seen["store"].host, seen["store"].port, seen["store"].secure) == ("clickhouse.example", 8443, True)
    table = (tmp_path / "steps.txt").read_text(encoding="utf-8")
    for slug in ("observe", "hyperdx", "refused"):
        assert slug in table.split("FAILED:", 1)[1]


def test_the_runner_needs_the_list_the_egress_and_the_cluster() -> None:
    with pytest.raises(SystemExit):
        edge_run.build_parser().parse_args(["--ui-url", "https://dfe.example", "--engine-url", "https://dfe.example"])
    args = edge_run.build_parser().parse_args([
        "--ui-url", "https://dfe.example", "--engine-url", "https://dfe.example", "--namespace", "dfe-run",
        "--kubectl", "kubectl --kubeconfig kc", "--allow", SENDER, "--egress-cidrs", EGRESS,
    ])
    assert (args.allow, args.egress_cidrs, args.gateway_address, args.hyperdx_url) == ([SENDER], EGRESS, "", "")


def test_the_report_names_the_failed_rows_and_carries_no_secret(tmp_path: Path) -> None:
    results = [wizard.StepResult("accepted", "done", "202"), wizard.StepResult("hyperdx", "failed", "token sekrit-1 refused")]
    text = edge_run.report(results, tmp_path, secrets=("sekrit-1",))
    assert "FAILED: hyperdx" in text
    assert "sekrit-1" not in text


# --- dfe-engine's minted secrets ---------------------------------------------------------------

PREFIX = "dfe/test/engine"
KEY = "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"
RUN = {"dfe-e2e": "r20261016t000000z-abcdef", "expires-at": "2026-10-16T06:00:00Z"}
FACTS = {
    "dfe.hyperi.io/engine_secrets_prefix": PREFIX,
    "dfe.hyperi.io/kms_key_arn": KEY,
    "dfe.hyperi.io/region": "us-west-2",
    "dfe.hyperi.io/run_tags": json.dumps(RUN),
    "dfe.hyperi.io/resource_tags": json.dumps({**RUN, "service-name": "dfe"}),
}


def _entry(path: str, *, key: str = KEY, tags: dict | None = None) -> dict:
    """One ListSecrets entry: what the API answers about a secret, never its value."""
    carried = RUN if tags is None else tags
    return {
        "Name": f"{PREFIX}/{path}",
        "KmsKeyId": key,
        "Tags": [{"Key": k, "Value": v} for k, v in carried.items()],
    }


MINTED_ENTRIES = [_entry("jwt/signing-key"), _entry("ch/service/query_reader"), _entry("ch/orgs/acceptance")]


def test_the_engine_store_is_secrets_manager_under_its_prefix_key_and_run() -> None:
    status, detail = edge.minted_outcome([], MINTED_ENTRIES, PREFIX, KEY, RUN)
    assert status == "done", detail
    assert "jwt/signing-key, ch/service/query_reader, ch/orgs/acceptance" in detail


def test_a_key_listed_by_its_id_is_the_same_key() -> None:
    entries = [_entry(e["Name"].removeprefix(f"{PREFIX}/"), key=KEY.rsplit("/", 1)[1]) for e in MINTED_ENTRIES]
    assert edge.minted_outcome([], entries, PREFIX, KEY, RUN)[0] == "done"


@pytest.mark.parametrize(
    ("files", "entries", "said"),
    [
        (["/config/.secrets/jwt/signing-key"], MINTED_ENTRIES, "1 file(s) under /config/.secrets"),
        ([], MINTED_ENTRIES[1:], f"holds no {PREFIX}/jwt/signing-key"),
        ([], MINTED_ENTRIES[:2], f"holds no {PREFIX}/ch/orgs/<org>"),
        ([], [*MINTED_ENTRIES[:2], _entry("ch/orgs/acceptance", key="")], "the AWS managed key"),
        ([], [*MINTED_ENTRIES[:2], _entry("ch/orgs/acceptance", key=KEY.replace("0000-0000-0000", "1111-1111-1111"))], "not arn:"),
        ([], [*MINTED_ENTRIES[:2], _entry("ch/orgs/acceptance", tags={})], "does not carry dfe-e2e, expires-at"),
    ],
    ids=["a-file-left-on-the-volume", "no-signing-key", "no-org-password", "aws-managed-key", "another-key", "no-run-tag"],
)
def test_expected_fail_the_store_is_named_where_it_falls_short(files: list, entries: list, said: str) -> None:
    status, detail = edge.minted_outcome(files, entries, PREFIX, KEY, RUN)
    assert status == "failed"
    assert said in detail, detail


class Engine:
    """kubectl and the aws CLI as the row calls them, each answer fixed."""

    def __init__(self, facts: dict | None = FACTS, find: tuple[int, str, str] = (0, "", ""),
                 listing: tuple[int, str, str] | None = None) -> None:
        self.facts = facts
        self.find = find
        self.listing = listing or (0, json.dumps({"SecretList": MINTED_ENTRIES}), "")
        self.kubectl: list[tuple[str, ...]] = []
        self.aws: list[list[str]] = []

    def kubectl_call(self, kube: list[str], *argv: str, stdin: str = "") -> tuple[int, str, str]:
        self.kubectl.append(argv)
        if "secret" in argv:
            if self.facts is None:
                return 1, "", 'Error from server (Forbidden): secrets "dfe-cluster" is forbidden'
            return 0, json.dumps(self.facts), ""
        return self.find

    def run(self, cmd: list[str], **_k: object) -> types.SimpleNamespace:
        self.aws.append(cmd)
        rc, out, err = self.listing
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _minted(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> list[wizard.StepResult]:
    monkeypatch.setattr(edge, "kubectl", engine.kubectl_call)
    monkeypatch.setattr(edge.subprocess, "run", engine.run)
    results: list[wizard.StepResult] = []
    edge.record_minted_secrets(results, ["kubectl"], "dfe-run")
    return results


def test_the_row_reads_annotations_names_and_a_listing_and_never_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = Engine()
    (result,) = _minted(monkeypatch, engine)
    assert (result.slug, result.status) == ("minted-secrets-in-store", "done"), result.detail
    secret_read, find = engine.kubectl
    assert secret_read[-2:] == ("-o", "jsonpath={.metadata.annotations}")
    assert find[-5:] == ("--", "find", "/config/.secrets", "-type", "f")
    ((aws),) = engine.aws
    assert aws[:3] == ["aws", "secretsmanager", "list-secrets"]
    assert aws[aws.index("--region") + 1] == "us-west-2"
    assert aws[aws.index("--filters") + 1] == f"Key=name,Values={PREFIX}/"
    assert not any("get-secret-value" in part for call in engine.aws for part in call)


def test_a_store_the_engine_never_created_holds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = Engine(find=(1, "", "find: '/config/.secrets': No such file or directory\ncommand terminated with exit code 1"))
    (result,) = _minted(monkeypatch, engine)
    assert result.status == "done", result.detail


def test_off_aws_the_row_is_skipped_and_nothing_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = Engine(facts={"dfe.hyperi.io/cloud": "local"})
    (result,) = _minted(monkeypatch, engine)
    assert result.status == "skipped"
    assert "every cloud but AWS" in result.detail
    assert len(engine.kubectl) == 1
    assert engine.aws == []


@pytest.mark.parametrize(
    ("engine", "said"),
    [
        (Engine(facts=None), "could not read secret/dfe-cluster"),
        (Engine(find=(1, "", 'Error from server (NotFound): deployments.apps "dfe-engine" not found')), "could not list /config/.secrets"),
        (Engine(listing=(255, "", "An error occurred (AccessDeniedException) when calling the ListSecrets operation")), "list-secrets failed"),
        (Engine(facts={**FACTS, "dfe.hyperi.io/run_tags": "{not json"}), "tags are not JSON"),
    ],
    ids=["cluster-secret-unreadable", "engine-not-found", "listing-refused", "tags-not-json"],
)
def test_expected_fail_a_row_that_cannot_look_fails_rather_than_skips(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, said: str,
) -> None:
    (result,) = _minted(monkeypatch, engine)
    assert result.status == "failed"
    assert said in result.detail, result.detail


def test_a_case_twin_under_another_deployment_is_never_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The name filter is case-insensitive; DFE/Test/engine/ is another deployment's."""
    twin = {**_entry("jwt/signing-key"), "Name": "DFE/Test/engine/jwt/signing-key"}
    engine = Engine(listing=(0, json.dumps({"SecretList": [twin, *MINTED_ENTRIES[1:]]}), ""))
    (result,) = _minted(monkeypatch, engine)
    assert result.status == "failed"
    assert f"holds no {PREFIX}/jwt/signing-key" in result.detail


def test_the_runner_records_the_store_after_hyperdx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before: list[str] = []
    monkeypatch.setattr(edge_run, "ingest_rows", lambda results, *_a: (edge.row(results, "public-endpoint", "failed", "no door"), (None, False, False))[1])
    monkeypatch.setattr(edge, "record_minted_secrets", lambda results, kube, namespace: before.extend([namespace, *(r.slug for r in results)]))
    monkeypatch.setenv("DFE_E2E_ADMIN_PASSWORD", "minted")
    args = edge_run.build_parser().parse_args([
        "--ui-url", "https://dfe.example", "--engine-url", "https://dfe.example", "--namespace", "dfe-run",
        "--kubectl", "kubectl", "--allow", SENDER, "--egress-cidrs", EGRESS, "--shots-dir", str(tmp_path),
    ])
    edge_run.run(args)
    assert before == ["dfe-run", "public-endpoint", "observe", "hyperdx"]
