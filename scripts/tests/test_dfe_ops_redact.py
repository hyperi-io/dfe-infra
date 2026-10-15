#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_redact.py
#  Purpose:      Guard `dfe-ops redact`: a provider error body, bearer tokens and private
#                key blocks come out masked, ordinary tofu output passes untouched, the
#                stream is read and written a line at a time in bounded memory, and the
#                documented pipeline keeps the producer's exit status.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe_ops_redact.py and its place in dfe-ops.

    python3 -m pytest scripts/tests/test_dfe_ops_redact.py -q

The filter tests feed `redact_stream` bytes and read back bytes. The CLI tests run the
real `dfe-ops redact` as a child process on real pipes; no cluster is involved.
"""

import importlib.machinery
import importlib.util
import io
import json
import select
import shlex
import signal
import subprocess
import sys
import tracemalloc
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import dfe_ops_failure_evidence as fe  # noqa: E402
import dfe_ops_redact as rd  # noqa: E402

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_redact", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_redact", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_redact"] = dfeops
_loader.exec_module(dfeops)

# Built from parts so no literal here looks like a credential to a secret scanner.
FAKE = "hunter2-FAKE-" + "not-a-credential"
PEM_BEGIN = "-----BEGIN " + "RSA PRIVATE KEY-----"
PEM_END = "-----END " + "RSA PRIVATE KEY-----"
MASK = fe.MASK

# A Confluent provider error that quotes the key it failed to create, secret included.
CONFLUENT_BODY = {
    "errors": [{"id": "9f2c", "status": "403", "detail": "API key quota exceeded"}],
    "id": "FAKEKEYID1234567",
    "spec": {"secret": FAKE, "owner": {"id": "sa-abc12", "kind": "ServiceAccount"}},
}

TOFU_OUTPUT = [
    "module.eks.aws_eks_cluster.this: Still creating... [10s elapsed]",
    "aws_vpc.main: Creation complete after 12s [id=vpc-0abc123def456]",
    "  # aws_s3_bucket.logs will be created",
    '  + resource "aws_s3_bucket" "logs" {',
    '      + bucket = "dfe-cycle-logs"',
    "Plan: 3 to add, 0 to change, 0 to destroy.",
    "Apply complete! Resources: 3 added, 0 changed, 0 destroyed.",
    "Outputs:",
    'cluster_endpoint = "https://ABCDEF.gr7.ap-southeast-2.eks.amazonaws.com"',
    'kafka_api_key_id = "FAKEKEYID1234567"',
    "Error: creating EKS cluster: operation error EKS: CreateCluster, https response error",
    "  on main.tf line 12, in resource \"aws_eks_cluster\" \"this\":",
    "",
]


def stream(data: bytes) -> bytes:
    """`data` through the filter."""
    out = io.BytesIO()
    rd.redact_stream(io.BytesIO(data), out)
    return out.getvalue()


def text(data: str) -> str:
    return stream(data.encode()).decode()


# --- what comes out masked ----------------------------------------------------------------


@pytest.mark.parametrize("indent", [None, 2], ids=["one line", "pretty printed"])
def test_a_confluent_error_body_comes_out_without_its_secret(indent: int | None) -> None:
    body = json.dumps(CONFLUENT_BODY, indent=indent)
    out = text(f"Error: creating Confluent API key: 403 Forbidden: {body}\n")
    assert FAKE not in out
    assert f'"secret": "{MASK}"' in out
    for kept in ("API key quota exceeded", "FAKEKEYID1234567", "sa-abc12", "creating Confluent"):
        assert kept in out


def test_a_confluent_error_body_quoted_inside_a_string_comes_out_masked_too() -> None:
    quoted = json.dumps(json.dumps(CONFLUENT_BODY))[1:-1]
    out = text(f'Error: provider returned {{"detail": "{quoted}"}}\n')
    assert FAKE not in out
    assert MASK in out


def test_a_bearer_token_comes_out_masked() -> None:
    out = text(f"got 401 for bearer {FAKE} from https://api.example/v2/api-keys\n")
    assert FAKE not in out
    assert out == f"got 401 for bearer {MASK} from https://api.example/v2/api-keys\n"
    assert FAKE not in text(f"Authorization: Bearer {FAKE}\n")


def test_a_private_key_block_comes_out_masked_whatever_its_length() -> None:
    body = "\n".join(f"{FAKE}-{n}" for n in range(500))
    out = text(f"before\n{PEM_BEGIN}\n{body}\n{PEM_END}\nafter\n")
    assert out == f"before\n{MASK}\nafter\n"


def test_a_block_that_never_ends_is_masked_to_the_end_of_input() -> None:
    out = text(f"before\n{PEM_BEGIN}\n{FAKE}\nMORE\n")
    assert out == f"before\n{MASK}"


def test_a_block_that_starts_and_ends_mid_line_keeps_the_text_around_it() -> None:
    assert text(f"key {PEM_BEGIN}\n{FAKE}\n{PEM_END} tail\nmore\n") == f"key {MASK} tail\nmore\n"
    assert text(f"a {PEM_BEGIN} {FAKE} {PEM_END} b\n") == f"a {MASK} b\n"


def test_two_blocks_are_masked_one_after_the_other() -> None:
    out = text(f"{PEM_BEGIN}\nA{FAKE}\n{PEM_END}\nmid\n{PEM_BEGIN}\nB{FAKE}\n{PEM_END}\nend\n")
    assert out == f"{MASK}\nmid\n{MASK}\nend\n"


def test_an_end_marker_with_no_begin_masks_its_own_line() -> None:
    assert text(f"before\n{PEM_END}\nafter\n") == f"before\n{MASK}\nafter\n"


# --- what passes through ------------------------------------------------------------------


def test_ordinary_tofu_output_passes_through_unchanged() -> None:
    data = "\n".join(TOFU_OUTPUT) + "\n"
    assert stream(data.encode()) == data.encode()


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\n",
        b"\n\n",
        b"no newline at all",
        b"ok\nlast line has no newline",
        b"crlf line\r\nnext\r\n",
        b"\xff\xfe not utf-8\n",
    ],
)
def test_the_line_structure_survives(data: bytes) -> None:
    out = stream(data)
    assert out == fe.redact_bytes(data)
    assert out.count(b"\n") == data.count(b"\n")


def test_input_with_no_trailing_newline_is_redacted_and_gets_none() -> None:
    assert text(f"password={FAKE}") == f"password={MASK}"
    assert text(f"first\npassword={FAKE}") == f"first\npassword={MASK}"
    assert text("Apply complete!") == "Apply complete!"


@pytest.mark.parametrize(
    "data",
    [
        f"before\npassword={FAKE} rest\nafter\n",
        f'{{"level":"error","password":"{FAKE}","msg":"x"}}\n',
        f"curl --password {FAKE} --user bob\n",
        f"connect to postgres://dfe:{FAKE}@db.example:5432/x failed\n",
        f"password={FAKE}\r\nnext\r\n",
        f"{PEM_BEGIN}\n{FAKE}\nMORE\n{PEM_END}\nafter",
        f"x {PEM_BEGIN}\n{FAKE}\n{PEM_END} tail\nmore\n",
        f"a {PEM_BEGIN} {FAKE} {PEM_END} b\n",
        f"{PEM_BEGIN}\nA\n{PEM_END}\nmid\n{PEM_BEGIN}\nB\n{PEM_END}\nend\n",
        f"before\n{PEM_BEGIN}\n{FAKE}\n",
        json.dumps(CONFLUENT_BODY, indent=2) + "\n",
    ],
)
def test_the_stream_matches_the_redactor_run_over_the_whole_text(data: str) -> None:
    """Lines reach `redact` one at a time, so only a block spanning lines needs care."""
    assert text(data) == fe.redact(data)


# --- one line at a time, in bounded memory -------------------------------------------------


class _Source:
    """A readable that makes each line anew on demand and logs each read in `events`."""

    def __init__(self, line: bytes, count: int, events: list[str] | None = None) -> None:
        self.line, self.left, self.events = line, count, events

    def readline(self, limit: int = -1) -> bytes:
        if self.events is not None:
            self.events.append("read")
        if not self.left:
            return b""
        self.left -= 1
        # A copy, so a reader that keeps its lines holds as many bytes as it was given.
        return bytes(bytearray(self.line))


class _Sink:
    """A writable that keeps counts, not the bytes, and logs each call in `events`."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.size = self.lines = 0
        self.leaked = False
        self.events = events

    def write(self, data: bytes) -> int:
        if self.events is not None:
            self.events.append("write")
        self.size += len(data)
        self.lines += data.count(b"\n")
        self.leaked = self.leaked or FAKE.encode() in data
        return len(data)

    def flush(self) -> None:
        if self.events is not None:
            self.events.append("flush")


def test_each_line_is_written_and_flushed_before_the_next_is_read() -> None:
    events: list[str] = []
    rd.redact_stream(_Source(b"x=1\n", 3, events), _Sink(events))
    assert events == ["read", "write", "flush"] * 3 + ["read"]


def test_a_large_input_streams_through_in_constant_memory() -> None:
    line = f"2026-10-15 INFO retry password={FAKE} attempt=7 {'x' * 400}\n".encode()
    count = 40_000
    source, sink = _Source(line, count), _Sink()
    tracemalloc.start()
    try:
        rd.redact_stream(source, sink)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert sink.lines == count
    assert not sink.leaked
    assert len(line) * count > 16 * 1024 * 1024
    assert peak < 1024 * 1024, f"peaked at {peak} bytes over {len(line) * count} bytes of input"


@pytest.fixture
def small_lines(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(rd, "MAX_LINE", 32)
    monkeypatch.setattr(rd, "CHUNK", 8)
    return 32


def test_a_line_past_the_cap_is_cut_and_the_rest_of_it_dropped(small_lines: int) -> None:
    out = stream(b"first\n" + b"a" * 100 + b"\nsecond\n")
    assert out == b"first\n" + b"a" * small_lines + b"***TRUNCATED***\nsecond\n"


def test_a_secret_cut_by_the_cap_does_not_come_out(small_lines: int) -> None:
    data = b"y" * 20 + b" password=" + FAKE.encode() + b"\nnext\n"
    out = stream(data)
    assert b"hunter2" not in out
    assert out == b"y" * 20 + f" password={MASK}".encode() + b"***TRUNCATED***\nnext\n"


@pytest.mark.parametrize("tail", [b"\nnext\n", b""], ids=["newline", "end of input"])
def test_a_line_exactly_at_the_cap_is_not_cut(small_lines: int, tail: bytes) -> None:
    data = b"a" * small_lines + tail
    assert stream(data) == data


def test_a_line_one_past_the_cap_at_the_end_of_input_is_cut(small_lines: int) -> None:
    assert stream(b"a" * (small_lines + 1)) == b"a" * small_lines + b"***TRUNCATED***\n"


# --- the command ---------------------------------------------------------------------------


def _ready(stream_: io.BufferedReader, seconds: float = 30.0) -> bool:
    return bool(select.select([stream_], [], [], seconds)[0])


def _redact_proc() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(SCRIPT), "redact"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_a_line_comes_out_before_the_input_ends() -> None:
    with _redact_proc() as proc:
        proc.stdin.write(f"password={FAKE}\n".encode())
        proc.stdin.flush()
        assert _ready(proc.stdout), "no output while stdin was still open"
        assert proc.stdout.readline() == f"password={MASK}\n".encode()
        proc.stdin.write(b"second\n")
        proc.stdin.flush()
        assert _ready(proc.stdout)
        assert proc.stdout.readline() == b"second\n"
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
        assert proc.stderr.read() == b""


@pytest.mark.skipif(not hasattr(signal, "SIGINT"), reason="needs POSIX signals")
@pytest.mark.parametrize("name", rd.IGNORED_SIGNALS)
def test_a_signal_to_the_pipeline_does_not_end_the_filter_before_its_input_does(name: str) -> None:
    sent = getattr(signal, name)
    with _redact_proc() as proc:
        proc.stdin.write(b"one\n")
        proc.stdin.flush()
        assert _ready(proc.stdout)
        assert proc.stdout.readline() == b"one\n"
        proc.send_signal(sent)
        proc.stdin.write(f"two password={FAKE}\n".encode())
        proc.stdin.close()
        assert proc.stdout.read() == f"two password={MASK}\n".encode()
        assert proc.wait(timeout=30) == 0


def test_a_closed_stdout_ends_the_filter_without_a_traceback() -> None:
    pipeline = (
        f"yes 'a=b' | head -c 4000000 | {shlex.quote(sys.executable)} {shlex.quote(str(SCRIPT))} "
        "redact | head -n 1"
    )
    done = subprocess.run(["bash", "-c", pipeline], capture_output=True, timeout=120, check=False)
    assert done.stdout == b"a=b\n"
    assert done.stderr == b""


def _pipeline(pipefail: bool, produce: str) -> subprocess.CompletedProcess:
    """The documented usage line, run under bash, with `produce` standing in for tofu."""
    redact = f"{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPT))} redact"
    script = f"{'set -o pipefail; ' if pipefail else ''}({produce}) 2>&1 | {redact}"
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, timeout=120, check=False, text=True
    )


def test_pipefail_keeps_the_producers_exit_status_visible() -> None:
    produce = f"echo 'secret = {FAKE}'; echo 'Error: it failed' >&2; exit 3"
    done = _pipeline(pipefail=True, produce=produce)
    assert done.returncode == 3
    assert done.stdout == f"secret = {MASK}\nError: it failed\n"
    assert done.stderr == ""


def test_without_pipefail_the_pipeline_reports_the_filters_status() -> None:
    assert _pipeline(pipefail=False, produce="echo hello; exit 3").returncode == 0


def test_the_command_is_registered_and_prints_no_banner() -> None:
    parser = dfeops.build_parser()
    args = parser.parse_args(["redact"])
    assert args.func is rd.cmd_redact
    assert args.banner is False
    assert getattr(parser.parse_args(["probe", "GET", "http://localhost/"]), "banner", True)


@pytest.fixture
def keep_signals():
    names = [name for name in rd.IGNORED_SIGNALS if hasattr(signal, name)]
    saved = {name: signal.getsignal(getattr(signal, name)) for name in names}
    yield names
    for name, handler in saved.items():
        signal.signal(getattr(signal, name), handler)


def test_main_runs_the_filter_without_the_ephemeral_banner(
    monkeypatch: pytest.MonkeyPatch, keep_signals: list[str]
) -> None:
    def banner() -> str:
        pytest.fail("the ephemeral banner ran for the filter")

    out = io.BytesIO()
    monkeypatch.setattr(dfeops, "_ephemeral_notice", banner)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(f"token={FAKE}\n".encode())))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out))
    assert dfeops.main(["redact"]) == 0
    assert out.getvalue() == f"token={MASK}\n".encode()
    assert all(signal.getsignal(getattr(signal, name)) == signal.SIG_IGN for name in keep_signals)
