#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_bastion.py
#  Purpose:      Guard `dfe-ops bastion` -- the dial edit is surgical, up/down
#                shell out to render_dial.py and tofu in the right order,
#                forward refuses an unknown target and never writes
#                insecure-skip-tls-verify, and down's teardown proof reads
#                every one of the four checks it claims to.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe_ops_bastion.py.

    python3 -m pytest scripts/tests/test_dfe_ops_bastion.py -q

Every AWS CLI call is mocked at aws_cli.run_aws (the same boundary
test_cloud_sweep.py mocks); every tofu/render_dial.py call is mocked at this
module's own `_run` -- no network call and no real account is touched.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import dfe_ops_bastion as bastion  # noqa: E402

# ---------------------------------------------------------------------------
# _set_toolbox_field -- the dial editor
# ---------------------------------------------------------------------------

DIAL_WITH_TOOLBOX = """substrate: k8s

toolbox:
  ## The in-cluster troubleshooting pod, which has an `enabled` of its own.
  pod:
    enabled: false
    kubeApiAccess: false
    ttlSeconds: ""
  enabled: "false"
  aws:
    instance_type: t4g.small
    operator_role_arn: ""
  ttl_minutes: 60
  session:
    idle_timeout_minutes: 15
    max_duration_minutes: 240
  session_log_retention_days: 90

kafka:
  provider: strimzi
"""


def test_set_toolbox_field_flips_enabled_and_leaves_everything_else() -> None:
    updated = bastion._set_toolbox_field(DIAL_WITH_TOOLBOX, "enabled", "true")
    assert '  enabled: "true"' in updated
    # Every sibling field, and the block AFTER toolbox:, survive verbatim.
    assert "instance_type: t4g.small" in updated
    assert "ttl_minutes: 60" in updated
    assert "provider: strimzi" in updated


def test_set_toolbox_field_sets_ttl_minutes() -> None:
    updated = bastion._set_toolbox_field(DIAL_WITH_TOOLBOX, "ttl_minutes", "45")
    assert '  ttl_minutes: "45"' in updated


def test_set_toolbox_field_does_not_touch_a_same_named_field_outside_the_block() -> None:
    dial = DIAL_WITH_TOOLBOX + '\nother:\n  enabled: "false"\n'
    updated = bastion._set_toolbox_field(dial, "enabled", "true")
    lines = updated.splitlines()
    other_at = lines.index("other:")
    # toolbox.enabled flips; other.enabled (outside the toolbox: block) does not.
    assert '  enabled: "true"' in lines
    assert lines[other_at + 1] == '  enabled: "false"'


def test_set_toolbox_field_addresses_the_top_level_toggle_not_the_nested_one() -> None:
    """toolbox.pod.enabled comes FIRST in the dial and is not the bastion's own
    toggle -- matching on the bare key edited it and left the real one alone."""
    updated = bastion._set_toolbox_field(DIAL_WITH_TOOLBOX, "enabled", "true")
    assert '  enabled: "true"' in updated.splitlines()
    assert "    enabled: false" in updated.splitlines(), "toolbox.pod.enabled must survive verbatim"


def test_set_toolbox_field_reaches_the_nested_key_by_its_dotted_path() -> None:
    updated = bastion._set_toolbox_field(DIAL_WITH_TOOLBOX, "pod.enabled", "true")
    assert '    enabled: "true"' in updated.splitlines()
    assert '  enabled: "false"' in updated.splitlines(), "the bastion toggle must survive verbatim"


def test_set_toolbox_field_refuses_a_path_the_block_does_not_carry() -> None:
    with pytest.raises(bastion.BastionError, match=r"toolbox\.aws\.enabled"):
        bastion._set_toolbox_field(DIAL_WITH_TOOLBOX, "aws.enabled", "true")


def test_up_and_down_both_address_the_top_level_toggle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The live cycle hit this in BOTH directions, so both are pinned here."""
    dial = tmp_path / "deployment.yaml"
    for enabled in (True, False):
        dial.write_text(DIAL_WITH_TOOLBOX, encoding="utf-8")
        monkeypatch.setattr(bastion, "DIAL", dial)
        bastion._set_toolbox_enabled(enabled)
        lines = dial.read_text(encoding="utf-8").splitlines()
        assert f'  enabled: "{str(enabled).lower()}"' in lines
        assert "    enabled: false" in lines


def test_set_toolbox_field_refuses_a_dial_with_no_toolbox_block() -> None:
    with pytest.raises(bastion.BastionError, match=r"toolbox\.enabled"):
        bastion._set_toolbox_field("substrate: k8s\nkafka:\n  provider: strimzi\n", "enabled", "true")


def test_set_toolbox_field_refuses_an_unknown_field() -> None:
    with pytest.raises(bastion.BastionError, match=r"toolbox\.bogus_field"):
        bastion._set_toolbox_field(DIAL_WITH_TOOLBOX, "bogus_field", "true")


# ---------------------------------------------------------------------------
# Shared mocking helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_admin_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads or writes the repo's own recorded admin peer, so a
    developer who has actually joined a hub does not change what these assert."""
    monkeypatch.setattr(bastion, "ADMIN_STATE", tmp_path / "isolated-admin-peer.json")


def _ok(stdout_obj: object = None) -> subprocess.CompletedProcess:
    stdout = json.dumps(stdout_obj) if stdout_obj is not None else ""
    return subprocess.CompletedProcess(args=["aws"], returncode=0, stdout=stdout, stderr="")


def _fail() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["aws"], returncode=1, stdout="", stderr="boom")


def _mock_aws(monkeypatch: pytest.MonkeyPatch, *responses: subprocess.CompletedProcess) -> list[list[str]]:
    """Replace aws_cli.run_aws with one that returns `responses` in call order
    and records each call's argv."""
    queue = list(responses)
    calls: list[list[str]] = []

    def fake_run_aws(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return queue.pop(0)

    monkeypatch.setattr(bastion.aws_cli, "run_aws", fake_run_aws)
    return calls


def _mock_run(monkeypatch: pytest.MonkeyPatch, *responses: subprocess.CompletedProcess) -> list[list[str]]:
    """Replace this module's own tofu/render_dial subprocess boundary."""
    queue = list(responses)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return queue.pop(0)

    monkeypatch.setattr(bastion, "_run", fake_run)
    return calls


TOOLBOX_OUTPUTS = {
    "toolbox_instance_id": {"value": "i-0123456789abcdef0"},
    "toolbox_ssm_session_document": {"value": "dfe-test-toolbox-shell"},
    "toolbox_targets": {
        "value": {
            "eks-api": {
                "host": "abc123.gr7.us-west-2.eks.amazonaws.com",
                "port": 443,
                "document_name": "dfe-test-toolbox-forward-eks-api",
            },
            "kafka": {
                "host": "b-1.mock.kafka.us-west-2.amazonaws.com",
                "port": 9096,
                "document_name": "dfe-test-toolbox-forward-kafka",
            },
        }
    },
    "cluster_name": {"value": "dfe-test"},
    "DFE_REGION": {"value": "us-west-2"},
}


def _mock_outputs(monkeypatch: pytest.MonkeyPatch, outputs: dict[str, object] = TOOLBOX_OUTPUTS) -> None:
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: outputs)


# ---------------------------------------------------------------------------
# up
# ---------------------------------------------------------------------------


def _dial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str = DIAL_WITH_TOOLBOX) -> Path:
    dial = tmp_path / "deployment.yaml"
    dial.write_text(text, encoding="utf-8")
    monkeypatch.setattr(bastion, "DIAL", dial)
    return dial


def _args(**overrides: object) -> bastion.argparse.Namespace:
    import argparse

    base: dict[str, object] = {
        "ttl": None, "wait_timeout": 1.0, "target": None, "local_port": None,
        "namespace": "dfe", "peer": None, "port": 22,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# The hub -- a fake culvert behind the one kubectl boundary
# ---------------------------------------------------------------------------

ISSUED_CONFIG = """[Interface]
PrivateKey = YOUR_PRIVATE_KEY_HERE
Address = 100.64.2.5/32
DNS = 1.1.1.1, 1.0.0.1
MTU = 1420

[Peer]
PublicKey = c2VydmVycHVibGlja2V5MDAwMDAwMDAwMDAwMDAwMDAw
Endpoint = vpn.example.com:51820
AllowedIPs = 198.19.0.0/16, 100.64.2.0/24
PersistentKeepalive = 25
"""

APPLIANCE_KEY = "YXBwbGlhbmNlMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAw"
ADMIN_KEY = "YWRtaW4wMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMA"


class FakeCulvert:
    """culvert's pod, answering the calls the hub verbs actually make.

    Mutable, so a revocation really removes the peer and the proof that follows
    it reads the changed state rather than a fixture that never moves.
    """

    def __init__(self, *, tunnel_address: str = "198.51.100.7", handshake: str = "1789000000",
                 pod_ip: str = "10.20.30.40", wg_network: str = "100.64.2.0/24") -> None:
        self.tunnel_address = tunnel_address
        self.allocations = {"hub-appliance-1": "100.64.2.2", bastion.ADMIN_PEER_NAME: "100.64.2.5"}
        self.allowed = {APPLIANCE_KEY: "100.64.2.2/32", ADMIN_KEY: "100.64.2.5/32"}
        self.handshakes = {APPLIANCE_KEY: handshake, ADMIN_KEY: handshake}
        self.pod_ip = pod_ip
        self.wg_network = wg_network
        self.revoke_sticks = True
        self.calls: list[list[str]] = []

    def _table(self, rows: dict[str, str]) -> str:
        return "".join(f"{key}\t{value}\n" for key, value in rows.items())

    def _pod(self) -> dict[str, object]:
        env = [{"name": bastion.WG_NETWORK_ENV, "value": self.wg_network}] if self.wg_network else []
        return {
            "status": {"podIP": self.pod_ip},
            "spec": {"containers": [{"name": "culvert", "env": env}]},
        }

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(args)
        if "pod" in args and "json" in args:
            return _text(json.dumps(self._pod()))
        if "pods" in args:
            return _text("dfe-culvert-7d9f")
        if bastion.CLUSTER_SECRET in args:
            return _text(self.tunnel_address)
        if "allowed-ips" in args:
            return _text(self._table(self.allowed))
        if "latest-handshakes" in args:
            return _text(self._table(self.handshakes))
        if bastion.CULVERT_ALLOCATIONS in args:
            return _text(json.dumps(self.allocations))
        if "generate-client" in args:
            return _text("")
        if f"{bastion.CULVERT_CLIENTS}/{bastion.ADMIN_PEER_NAME}-wg-split.conf" in args:
            return _text(ISSUED_CONFIG)
        if "revoke-client" in args:
            if self.revoke_sticks:
                self.allocations.pop(bastion.ADMIN_PEER_NAME, None)
                self.allowed.pop(ADMIN_KEY, None)
            return _text("")
        return _text("")


def _text(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["kubectl"], returncode=0, stdout=stdout, stderr="")


def _mock_kubectl(monkeypatch: pytest.MonkeyPatch, hub: FakeCulvert | None = None) -> FakeCulvert:
    hub = hub or FakeCulvert()
    monkeypatch.setattr(bastion, "_kubectl", hub)
    return hub


def _ssm(*outputs: str) -> list[subprocess.CompletedProcess]:
    """One send-command plus one get-command-invocation per Run Command."""
    responses: list[subprocess.CompletedProcess] = []
    for out in outputs:
        responses.append(_ok({"Command": {"CommandId": "c-0123456789abcdef0"}}))
        responses.append(_ok({"Status": "Success", "StandardOutputContent": out}))
    return responses


def _mock_admin_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "bastion-admin-peer.json"
    monkeypatch.setattr(bastion, "ADMIN_STATE", state)
    return state


def _join_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The join path as it behaves once hyperi-io/culvert#40 lands, which is
    what keeps it exercised while the verb itself refuses."""
    monkeypatch.setattr(bastion, "ADMIN_PEER_SUPPORTED", True)


# ---------------------------------------------------------------------------
# peers
# ---------------------------------------------------------------------------


def test_peers_joins_the_allocations_to_the_live_handshakes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _mock_kubectl(monkeypatch)
    rc = bastion.cmd_bastion_peers(_args())
    err = capsys.readouterr().err

    assert rc == 0
    assert "hub-appliance-1" in err
    assert "100.64.2.2" in err
    assert "2026-" in err


def test_peers_never_reads_the_file_carrying_the_servers_private_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wg0.conf would name every peer in one read and carries the server key."""
    hub = _mock_kubectl(monkeypatch)
    bastion.cmd_bastion_peers(_args())
    assert not any("wg0.conf" in " ".join(call) for call in hub.calls)


def test_peers_reports_a_peer_that_has_never_handshaken(monkeypatch: pytest.MonkeyPatch,
                                                        capsys: pytest.CaptureFixture) -> None:
    hub = FakeCulvert()
    hub.handshakes[APPLIANCE_KEY] = "0"
    _mock_kubectl(monkeypatch, hub)
    bastion.cmd_bastion_peers(_args())
    assert "never" in capsys.readouterr().err


def test_peers_refuses_when_the_tunnel_is_not_deployed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(bastion, "_kubectl", lambda args: _text(""))
    assert bastion.cmd_bastion_peers(_args()) == 1
    assert "no running culvert pod" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------


def test_admin_config_pins_the_endpoint_drops_dns_and_narrows_allowed_ips() -> None:
    out = bastion._admin_config(ISSUED_CONFIG, "198.51.100.7")
    assert "Endpoint = 198.51.100.7:51820" in out
    assert "DNS = " not in out
    assert "AllowedIPs = 100.64.2.0/24" in out
    # The service range must not follow the tunnel, or the instance loses its
    # own route to anything the deployment runs in the VPC.
    assert "198.19.0.0/16" not in out
    assert bastion.PRIVATE_KEY_PLACEHOLDER in out


def test_join_refuses_and_names_the_upstream_change_it_waits_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A minted peer that every isolation rule then drops looks like a working
    join and reaches nothing, so the verb refuses and points at the route."""
    _mock_kubectl(monkeypatch)
    _mock_outputs(monkeypatch)
    assert bastion.cmd_bastion_join(_args()) == 1
    err = capsys.readouterr().err
    assert "hyperi-io/culvert#40" in err
    assert "bastion hub" in err


def test_join_mints_the_private_key_on_the_instance_and_ships_only_the_public_half(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _join_enabled(monkeypatch)
    hub = _mock_kubectl(monkeypatch)
    _mock_admin_state(tmp_path, monkeypatch)
    _mock_outputs(monkeypatch)
    aws_calls = _mock_aws(monkeypatch, *_ssm(ADMIN_KEY, "", f"{ADMIN_KEY}\t1789000000\n"))

    assert bastion.cmd_bastion_join(_args()) == 0

    # culvert is handed the PUBLIC key and nothing else of the pair.
    issue = next(call for call in hub.calls if "generate-client" in call)
    assert "--pubkey" in issue
    assert issue[issue.index("--pubkey") + 1] == ADMIN_KEY
    # Nothing sent to SSM carries a private key -- only the placeholder culvert
    # wrote, which the instance substitutes from its own file.
    sent = " ".join(" ".join(call) for call in aws_calls)
    assert bastion.PRIVATE_KEY_PLACEHOLDER in sent
    assert "wg genkey" in sent


def test_join_refuses_with_no_tunnel_address_on_the_cluster_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """byo brings an address this deployment never sees, so there is nothing to
    dial and no UDP egress rule aimed at it."""
    _join_enabled(monkeypatch)
    _mock_kubectl(monkeypatch, FakeCulvert(tunnel_address=""))
    _mock_admin_state(tmp_path, monkeypatch)
    _mock_outputs(monkeypatch)
    assert bastion.cmd_bastion_join(_args()) == 1
    assert "no dfe.hyperi.io/tunnel_address" in capsys.readouterr().err


def test_join_refuses_when_no_handshake_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """wg-quick reports success on a config that reaches nothing."""
    _join_enabled(monkeypatch)
    _mock_kubectl(monkeypatch)
    state = _mock_admin_state(tmp_path, monkeypatch)
    _mock_outputs(monkeypatch)
    _mock_aws(monkeypatch, *_ssm(ADMIN_KEY, "", f"{ADMIN_KEY}\t0\n"))

    assert bastion.cmd_bastion_join(_args()) == 1
    assert "completed no handshake" in capsys.readouterr().err
    assert not state.exists()


def test_join_records_the_peer_and_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _join_enabled(monkeypatch)
    _mock_kubectl(monkeypatch)
    state = _mock_admin_state(tmp_path, monkeypatch)
    _mock_outputs(monkeypatch)
    _mock_aws(monkeypatch, *_ssm(ADMIN_KEY, "", f"{ADMIN_KEY}\t1789000000\n"))

    assert bastion.cmd_bastion_join(_args(ttl=30)) == 0
    recorded = json.loads(state.read_text(encoding="utf-8"))
    assert recorded["name"] == bastion.ADMIN_PEER_NAME
    assert recorded["ttl_minutes"] == 30
    assert recorded["expires_at"] > 0
    assert state.stat().st_mode & 0o777 == 0o600


def test_join_refuses_without_an_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _join_enabled(monkeypatch)
    _mock_kubectl(monkeypatch)
    _mock_admin_state(tmp_path, monkeypatch)
    _mock_outputs(monkeypatch, {})
    assert bastion.cmd_bastion_join(_args()) == 1
    assert "bastion up" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# hub
# ---------------------------------------------------------------------------


def test_hub_routes_the_client_range_at_the_pod_then_opens_the_logged_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The route is what reaches the appliance, and it is re-programmed here
    rather than assumed, because a roll gives the pod a new address."""
    _mock_kubectl(monkeypatch)
    _mock_outputs(monkeypatch)
    aws_calls = _mock_aws(monkeypatch, *_ssm(""))
    calls: list[list[str]] = []
    monkeypatch.setattr(bastion, "_run_interactive", lambda cmd: calls.append(cmd) or 0)

    assert bastion.cmd_bastion_hub(_args(peer="hub-appliance-1", port=22)) == 0
    err = capsys.readouterr().err

    sent = " ".join(" ".join(call) for call in aws_calls)
    assert "ip route replace 100.64.2.0/24 via 10.20.30.40" in sent
    assert "100.64.2.2:22" in err
    assert calls[0][:3] == ["aws", "ssm", "start-session"]
    assert "--document-name" in calls[0]
    assert calls[0][calls[0].index("--document-name") + 1] == "dfe-test-toolbox-shell"


def test_hub_names_the_vpc_route_the_instance_route_does_not_cover(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A VPC delivers on the destination address, so the instance route alone
    reaches nothing and an operator hitting silence needs to be told where."""
    _mock_kubectl(monkeypatch)
    _mock_outputs(monkeypatch)
    _mock_aws(monkeypatch, *_ssm(""))
    monkeypatch.setattr(bastion, "_run_interactive", lambda cmd: 0)

    assert bastion.cmd_bastion_hub(_args(peer="hub-appliance-1", port=22)) == 0
    err = capsys.readouterr().err
    assert "source/destination check" in err


def test_hub_refuses_a_peer_outside_the_range_the_hub_forwards(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A route to a range the peer does not sit in is a reach-back that times
    out against a tunnel reporting healthy."""
    _mock_kubectl(monkeypatch, FakeCulvert(wg_network="100.64.9.0/24"))
    _mock_outputs(monkeypatch)
    assert bastion.cmd_bastion_hub(_args(peer="hub-appliance-1", port=22)) == 1
    assert "outside the 100.64.9.0/24" in capsys.readouterr().err


def test_hub_refuses_when_the_tunnel_runs_no_wireguard_listener(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _mock_kubectl(monkeypatch, FakeCulvert(wg_network=""))
    _mock_outputs(monkeypatch)
    assert bastion.cmd_bastion_hub(_args(peer="hub-appliance-1", port=22)) == 1
    assert "no CULVERT_WG_NETWORK" in capsys.readouterr().err


def test_hub_refuses_a_port_outside_the_admin_classs_reach(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _mock_kubectl(monkeypatch)
    _mock_outputs(monkeypatch)
    assert bastion.cmd_bastion_hub(_args(peer="hub-appliance-1", port=9000)) == 1
    assert "outside the admin class's reach" in capsys.readouterr().err


def test_hub_refuses_an_unknown_peer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _mock_kubectl(monkeypatch)
    _mock_outputs(monkeypatch)
    assert bastion.cmd_bastion_hub(_args(peer="not-a-peer", port=443)) == 1
    assert "unknown peer" in capsys.readouterr().err


def test_covers_reads_a_range_rather_than_matching_text() -> None:
    assert bastion._covers("100.64.2.0/24", "100.64.2.2")
    assert not bastion._covers("100.64.2.0/24", "100.64.20.2")
    assert not bastion._covers("100.64.2.0/24", "not-an-address")


# ---------------------------------------------------------------------------
# down -- revoke before terminate
# ---------------------------------------------------------------------------


def _joined_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = _mock_admin_state(tmp_path, monkeypatch)
    state.write_text(
        json.dumps({"name": bastion.ADMIN_PEER_NAME, "namespace": "dfe",
                    "ttl_minutes": 60, "expires_at": 1789003600}),
        encoding="utf-8",
    )
    return state


def test_down_revokes_the_admin_peer_before_the_destroy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _dial_file(tmp_path, monkeypatch, text=DIAL_WITH_TOOLBOX.replace('enabled: "false"', 'enabled: "true"'))
    state = _joined_state(tmp_path, monkeypatch)
    hub = _mock_kubectl(monkeypatch)
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: TOOLBOX_OUTPUTS)
    _mock_run(monkeypatch, subprocess.CompletedProcess(args=["render"], returncode=0),
              subprocess.CompletedProcess(args=["tofu"], returncode=0))
    _mock_aws(monkeypatch, _ok(["terminated"]), _ok({"Sessions": []}), _ok([]), _ok([]))

    rc = bastion.cmd_bastion_down(_args())
    err = capsys.readouterr().err

    assert rc == 0
    assert "revoked and off the hub" in err
    assert "CLEAN" in err
    assert not state.exists()
    revoke_at = next(i for i, call in enumerate(hub.calls) if "revoke-client" in call)
    proof_at = next(i for i, call in enumerate(hub.calls) if "allowed-ips" in call)
    assert revoke_at < proof_at


def test_down_reports_non_zero_when_the_peer_survives_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A peer left on the hub is a credential nobody tracks, and the instance is
    still terminated -- it is what bills, and it holds the private key."""
    _dial_file(tmp_path, monkeypatch, text=DIAL_WITH_TOOLBOX.replace('enabled: "false"', 'enabled: "true"'))
    state = _joined_state(tmp_path, monkeypatch)
    hub = FakeCulvert()
    hub.revoke_sticks = False
    _mock_kubectl(monkeypatch, hub)
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: TOOLBOX_OUTPUTS)
    _mock_run(monkeypatch, subprocess.CompletedProcess(args=["render"], returncode=0),
              subprocess.CompletedProcess(args=["tofu"], returncode=0))
    _mock_aws(monkeypatch, _ok(["terminated"]), _ok({"Sessions": []}), _ok([]), _ok([]))

    rc = bastion.cmd_bastion_down(_args())
    err = capsys.readouterr().err

    assert rc == 1
    assert "still on the hub" in err
    assert "INCOMPLETE" in err
    assert state.exists()


def test_down_with_no_admin_peer_touches_the_cluster_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Nothing joined means nothing to revoke, and no kubectl call at all."""
    _dial_file(tmp_path, monkeypatch, text=DIAL_WITH_TOOLBOX.replace('enabled: "false"', 'enabled: "true"'))
    _mock_admin_state(tmp_path, monkeypatch)
    hub = _mock_kubectl(monkeypatch)
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: TOOLBOX_OUTPUTS)
    _mock_run(monkeypatch, subprocess.CompletedProcess(args=["render"], returncode=0),
              subprocess.CompletedProcess(args=["tofu"], returncode=0))
    _mock_aws(monkeypatch, _ok(["terminated"]), _ok({"Sessions": []}), _ok([]), _ok([]))

    assert bastion.cmd_bastion_down(_args()) == 0
    assert hub.calls == []
    assert "none joined from this machine" in capsys.readouterr().err


def test_up_sets_enabled_applies_and_waits_for_online(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dial = _dial_file(tmp_path, monkeypatch)
    tofu_calls = _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),  # render_dial.py --tofu
        subprocess.CompletedProcess(args=[], returncode=0),  # tofu apply
    )
    _mock_outputs(monkeypatch)
    aws_calls = _mock_aws(
        monkeypatch,
        _ok({"InstanceInformationList": [{"PingStatus": "Online"}]}),
    )

    rc = bastion.cmd_bastion_up(_args())

    assert rc == 0
    assert '  enabled: "true"' in dial.read_text(encoding="utf-8")
    assert any("render_dial.py" in str(part) for part in tofu_calls[0])
    assert "apply" in tofu_calls[1]
    assert "-target=module.toolbox" in tofu_calls[1]
    assert any("describe-instance-information" in call for call in aws_calls)


def test_up_targets_the_toolbox_module_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every extra target is one more resource whose failure aborts the apply
    with the instance already created and billing, and before the toolbox's own
    forward documents exist."""
    _dial_file(tmp_path, monkeypatch)
    tofu_calls = _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    _mock_outputs(monkeypatch)
    _mock_aws(monkeypatch, _ok({"InstanceInformationList": [{"PingStatus": "Online"}]}))

    assert bastion.cmd_bastion_up(_args()) == 0
    assert [part for part in tofu_calls[1] if str(part).startswith("-target=")] == [
        "-target=module.toolbox"
    ]


def test_up_with_ttl_overrides_ttl_minutes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dial = _dial_file(tmp_path, monkeypatch)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    _mock_outputs(monkeypatch)
    _mock_aws(monkeypatch, _ok({"InstanceInformationList": [{"PingStatus": "Online"}]}))

    rc = bastion.cmd_bastion_up(_args(ttl=30))

    assert rc == 0
    assert '  ttl_minutes: "30"' in dial.read_text(encoding="utf-8")


def test_up_fails_when_tofu_apply_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _dial_file(tmp_path, monkeypatch)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=1),
    )

    assert bastion.cmd_bastion_up(_args()) == 1


def test_up_fails_when_the_instance_never_goes_online(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dial_file(tmp_path, monkeypatch)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    _mock_outputs(monkeypatch)
    # A deterministic clock: three polls, then the deadline (10) is reached.
    # time.sleep is a no-op so the bounded wait loop runs instantly under test.
    clock = iter([0, 1, 2, 11])
    monkeypatch.setattr(bastion.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(bastion.time, "sleep", lambda _seconds: None)
    _mock_aws(monkeypatch, *([_ok({"InstanceInformationList": []})] * 3))

    assert bastion.cmd_bastion_up(_args(wait_timeout=10.0)) == 1


def test_up_refuses_a_dial_with_no_toolbox_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _dial_file(tmp_path, monkeypatch, text="substrate: k8s\nkafka:\n  provider: strimzi\n")
    assert bastion.cmd_bastion_up(_args()) == 1


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------


def test_shell_invokes_start_session_with_the_shell_document(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_outputs(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(bastion, "_run_interactive", lambda cmd: calls.append(cmd) or 0)

    rc = bastion.cmd_bastion_shell(_args())

    assert rc == 0
    assert calls[0] == [
        "aws", "ssm", "start-session",
        "--target", "i-0123456789abcdef0",
        "--document-name", "dfe-test-toolbox-shell",
    ]


def test_shell_refuses_when_no_instance_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_outputs(monkeypatch, {})
    assert bastion.cmd_bastion_shell(_args()) == 1


# ---------------------------------------------------------------------------
# forward
# ---------------------------------------------------------------------------


def test_forward_invokes_start_session_with_the_named_targets_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_outputs(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(bastion, "_run_interactive", lambda cmd: calls.append(cmd) or 0)

    rc = bastion.cmd_bastion_forward(_args(target="kafka", local_port=19096))

    assert rc == 0
    assert calls[0] == [
        "aws", "ssm", "start-session",
        "--target", "i-0123456789abcdef0",
        "--document-name", "dfe-test-toolbox-forward-kafka",
        "--parameters", "localPortNumber=19096",
    ]


def test_forward_refuses_an_unknown_target(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    _mock_outputs(monkeypatch)
    rc = bastion.cmd_bastion_forward(_args(target="not-a-real-target", local_port=8080))
    assert rc == 1
    assert "unknown target" in capsys.readouterr().err


def test_forward_prints_that_the_session_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _mock_outputs(monkeypatch)
    monkeypatch.setattr(bastion, "_run_interactive", lambda cmd: 0)
    bastion.cmd_bastion_forward(_args(target="kafka", local_port=19096))
    assert "NOT recorded" in capsys.readouterr().err


def test_forward_to_eks_api_writes_a_0600_kubeconfig_with_no_insecure_skip_tls_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_outputs(monkeypatch)
    monkeypatch.setattr(bastion, "_run_interactive", lambda cmd: 0)
    scratch = tmp_path / "toolbox-eks-api.kubeconfig"
    monkeypatch.setattr(bastion, "SCRATCH_KUBECONFIG", scratch)

    bastion.cmd_bastion_forward(_args(target="eks-api", local_port=18443))

    assert scratch.exists()
    mode = scratch.stat().st_mode & 0o777
    assert mode == 0o600
    content = scratch.read_text(encoding="utf-8")
    assert "server: https://127.0.0.1:18443" in content
    assert "tls-server-name: abc123.gr7.us-west-2.eks.amazonaws.com" in content
    assert "insecure-skip-tls-verify" not in content
    assert "--cluster-name" in content
    assert "dfe-test" in content


def test_forward_to_kafka_writes_no_kubeconfig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_outputs(monkeypatch)
    monkeypatch.setattr(bastion, "_run_interactive", lambda cmd: 0)
    scratch = tmp_path / "toolbox-eks-api.kubeconfig"
    monkeypatch.setattr(bastion, "SCRATCH_KUBECONFIG", scratch)

    bastion.cmd_bastion_forward(_args(target="kafka", local_port=19096))

    assert not scratch.exists()


# ---------------------------------------------------------------------------
# down -- the teardown proof
# ---------------------------------------------------------------------------


def test_down_flips_enabled_destroys_and_proves_clean_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dial = _dial_file(
        tmp_path,
        monkeypatch,
        text=DIAL_WITH_TOOLBOX.replace('enabled: "false"', 'enabled: "true"'),
    )
    # Outputs BEFORE the destroy (still carries the instance id) are read once,
    # then render_dial.py + tofu destroy run.
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: TOOLBOX_OUTPUTS)
    tofu_calls = _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    aws_calls = _mock_aws(
        monkeypatch,
        _ok(["terminated"]),  # describe-instances
        _ok({"Sessions": []}),  # describe-sessions
        _ok([]),  # describe-volumes
        _ok([]),  # describe-network-interfaces
    )

    rc = bastion.cmd_bastion_down(_args())

    assert rc == 0
    assert '  enabled: "false"' in dial.read_text(encoding="utf-8")
    assert len(aws_calls) == 4
    # A targeted DESTROY creates nothing, so a resource broken anywhere else in
    # the root cannot leave the instance running and billing.
    assert "destroy" in tofu_calls[1]
    assert "apply" not in tofu_calls[1]
    assert [part for part in tofu_calls[1] if str(part).startswith("-target=")] == [
        "-target=module.toolbox"
    ]


def test_down_fails_when_the_targeted_destroy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dial_file(tmp_path, monkeypatch, text=DIAL_WITH_TOOLBOX.replace('enabled: "false"', 'enabled: "true"'))
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: TOOLBOX_OUTPUTS)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=1),
    )

    assert bastion.cmd_bastion_down(_args()) == 1


def test_down_reports_non_zero_when_a_volume_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dial_file(tmp_path, monkeypatch, text=DIAL_WITH_TOOLBOX.replace('enabled: "false"', 'enabled: "true"'))
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: TOOLBOX_OUTPUTS)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    _mock_aws(
        monkeypatch,
        _ok(["terminated"]),
        _ok({"Sessions": []}),
        _ok(["vol-0123456789abcdef0"]),  # a volume survived
        _ok([]),
    )

    assert bastion.cmd_bastion_down(_args()) == 1


def test_down_reports_non_zero_when_the_instance_has_not_terminated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dial_file(tmp_path, monkeypatch, text=DIAL_WITH_TOOLBOX.replace('enabled: "false"', 'enabled: "true"'))
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: TOOLBOX_OUTPUTS)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    _mock_aws(
        monkeypatch,
        _ok(["shutting-down"]),  # not yet terminated
        _ok({"Sessions": []}),
        _ok([]),
        _ok([]),
    )

    assert bastion.cmd_bastion_down(_args()) == 1


def test_down_reports_non_zero_on_an_active_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _dial_file(tmp_path, monkeypatch, text=DIAL_WITH_TOOLBOX.replace('enabled: "false"', 'enabled: "true"'))
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: TOOLBOX_OUTPUTS)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    _mock_aws(
        monkeypatch,
        _ok(["terminated"]),
        _ok({"Sessions": [{"SessionId": "s-1"}]}),
        _ok([]),
        _ok([]),
    )

    assert bastion.cmd_bastion_down(_args()) == 1


def test_down_removes_the_scratch_kubeconfig_if_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dial_file(tmp_path, monkeypatch, text=DIAL_WITH_TOOLBOX.replace('enabled: "false"', 'enabled: "true"'))
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: TOOLBOX_OUTPUTS)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    _mock_aws(monkeypatch, _ok(["terminated"]), _ok({"Sessions": []}), _ok([]), _ok([]))

    scratch = tmp_path / "toolbox-eks-api.kubeconfig"
    scratch.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(bastion, "SCRATCH_KUBECONFIG", scratch)

    bastion.cmd_bastion_down(_args())

    assert not scratch.exists()


def test_down_with_no_prior_instance_reports_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """bastion down on a toolbox that was never brought up: nothing to prove
    wrong, and the two instance-scoped checks are never even queried."""
    _dial_file(tmp_path, monkeypatch)
    monkeypatch.setattr(bastion, "_tofu_outputs", dict)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    aws_calls = _mock_aws(monkeypatch, _ok([]), _ok([]))

    assert bastion.cmd_bastion_down(_args()) == 0
    assert len(aws_calls) == 2  # volumes + network interfaces only


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_reports_not_enabled_with_no_instance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _mock_outputs(monkeypatch, {})
    assert bastion.cmd_bastion_status(_args()) == 0
    assert "not enabled" in capsys.readouterr().err


def test_status_reports_ping_and_active_sessions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _mock_outputs(monkeypatch)
    _mock_aws(
        monkeypatch,
        _ok({"InstanceInformationList": [{"PingStatus": "Online"}]}),
        _ok({"Sessions": [{"SessionId": "s-1"}]}),
    )

    rc = bastion.cmd_bastion_status(_args())
    err = capsys.readouterr().err

    assert rc == 0
    assert "Online" in err
    assert "active sessions: 1" in err
    assert "eks-api" in err
    assert "kafka" in err
    assert "none joined from this machine" in err


def test_status_reports_an_admin_peer_past_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A recorded deadline is the only bound there is: culvert issues a
    WireGuard peer with no expiry of its own."""
    _mock_outputs(monkeypatch)
    state = _mock_admin_state(tmp_path, monkeypatch)
    state.write_text(
        json.dumps({"name": bastion.ADMIN_PEER_NAME, "namespace": "dfe",
                    "ttl_minutes": 60, "expires_at": 1}),
        encoding="utf-8",
    )
    _mock_aws(
        monkeypatch,
        _ok({"InstanceInformationList": [{"PingStatus": "Online"}]}),
        _ok({"Sessions": []}),
    )

    assert bastion.cmd_bastion_status(_args()) == 0
    assert "OVERDUE" in capsys.readouterr().err
