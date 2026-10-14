#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_internal_ca.py
#  Purpose:      Prove the private root persists across a rebuild and that the
#                export command tells an operator how to trust it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The internal-CA root: persistence render, export, and the issuer report (#238).

Every deploy used to mint a fresh private root, so every developer box had to
trust a new certificate after every rebuild and the embedded HyperDX iframe
failed outright -- an iframe cannot show the interstitial. Three things are
asserted here, because each fails silently:

- self-signed mode renders BOTH halves of the persistence pair against the same
  store entries (a PushSecret that saves the root, an ExternalSecret that
  restores it), and vault mode renders neither, since the estate PKI owns that
  root;
- the cert and the key each get a store entry of their own, because ESO checks
  IfNotExists per data entry and the AWS provider asks only whether the entry
  exists, and the restore takes a complete pair or writes nothing;
- `dfe-ops ca` prints the PUBLIC half and the install lines for both operating
  systems, and never the private key;
- `dfe-ops ca --status` distinguishes a restored root from a newly minted one,
  which is the line the readiness gate and the access summary carry;
- the install branch follows `platform.system()`, so a Mac never runs a
  Linux-only step and a Linux box is never told about the keychain.

    python3 scripts/tests/test_internal_ca.py
    python3 -m pytest scripts/tests/test_internal_ca.py

Needs `helm` on PATH. A fake `kubectl` first on PATH answers the cluster reads
from a JSON fixture. Runs standalone the way CI drives the other checks here,
and under pytest, where a failed expectation raises rather than counting.
"""

from __future__ import annotations

import base64
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = chart_dir("envoy-gateway-config")
COMMON_VALUES = REPO_ROOT / "argocd" / "values" / "common.yaml"
DFE_OPS = REPO_ROOT / "scripts" / "dfe-ops"


def _load_dfe_ops():
    """Import dfe-ops as a module -- it has no .py extension, so no import finds it."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_loader(
        "dfe_ops", importlib.machinery.SourceFileLoader("dfe_ops", str(DFE_OPS))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dfe_ops = _load_dfe_ops()

STORE_PATH = "dfe/local/pki/internal-ca"
CERT_ENTRY = f"{STORE_PATH}-crt"
KEY_ENTRY = f"{STORE_PATH}-key"

# A real ECDSA P-384 self-signed CA, so openssl parses it the way it parses the
# one cert-manager mints. Regenerating it changes nothing but the fixture.
ROOT_PEM = """-----BEGIN CERTIFICATE-----
MIIBtDCCATugAwIBAgIUKEstMWDSmzTOKP73VIdGIcz8/x0wCgYIKoZIzj0EAwMw
GjEYMBYGA1UEAxMPZGZlLWludGVybmFsLWNhMB4XDTI2MDkwNDEwMzIzMVoXDTM2
MDkwMTEwMzIzMVowGjEYMBYGA1UEAxMPZGZlLWludGVybmFsLWNhMHYwEAYHKoZI
zj0CAQYFK4EEACIDYgAEhCBU8adt1JhCqCthavfmIbKhrmxCc3wussJyJR6j6VCL
0/k5Gcd5htK9OZIQXHrUfVXLDELb8UQOD4ONjOtchbvL1BPYMUpOnV5NF/q/rVDT
W0S8HFBufKC8zT4dSvAAo0IwQDAOBgNVHQ8BAf8EBAMCAqQwDwYDVR0TAQH/BAUw
AwEB/zAdBgNVHQ4EFgQUvUiFhb9IBiitQYc1JJKnEM+cYxEwCgYIKoZIzj0EAwMD
ZwAwZAIwdR8VOWT/UmNrIubOYfwo9igrRWdYzIAEuIkXg4t1WnOmlmupkntzXrVq
yRgtWL6lAjAzY78bjaIKKS5ufYEL4yxZSRAu6UZ4RCOP1JzT3QP0Ug1yN/EIeMdx
cuLkgPhOaD0=
-----END CERTIFICATE-----
"""

PRIVATE_KEY_MARKER = "SECRETPRIVATEKEYMATERIAL"

# Answers the four reads `dfe-ops ca` makes, off FAKE_KUBECTL_FIXTURE.
FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8") as fh:
    fixture = json.load(fh)

def emit(doc):
    if doc is None:
        sys.exit(1)
    json.dump(doc, sys.stdout)

if "certificate" in args:
    emit(fixture.get("wildcard_certificate"))
elif "clusterissuer" in args:
    emit(fixture.get("clusterissuer"))
elif "externalsecret" in args:
    emit(fixture.get("restore_externalsecret"))
elif "secret" in args:
    emit((fixture.get("secrets") or {}).get(args[args.index("secret") + 1]))
else:
    sys.exit(1)
"""


def render(*sets: str) -> list[dict]:
    """The gateway chart's rendered documents for one set of values.

    Needs the real hostnames map (common.yaml) the same as any other cloud
    cascade: every route this chart enables by default now refuses to render
    with no hostname to publish it on, and this test's own concern (the
    internal-CA persistence pair) is unrelated to which routes exist.
    """
    cmd = ["helm", "template", "gw", str(CHART), "-f", str(COMMON_VALUES),
           "--set", "domain=dfe.example.com", "--set", "env=local"]
    for item in sets:
        cmd += ["--set", item]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {sets}:\n{out.stderr}")
    return [doc for doc in yaml.safe_load_all(out.stdout) if doc]


def by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def secret_doc(*keys_and_values: str) -> dict:
    """A Secret document with base64 data, the shape kubectl -o json returns."""
    data = {}
    for i in range(0, len(keys_and_values), 2):
        data[keys_and_values[i]] = base64.b64encode(
            keys_and_values[i + 1].encode()
        ).decode()
    return {"data": data}


BASE_FIXTURE = {
    "wildcard_certificate": {"spec": {"issuerRef": {"name": "dfe-internal-ca"}}},
    "clusterissuer": None,
    "restore_externalsecret": None,
    "secrets": {
        "dfe-internal-ca-tls": secret_doc(
            "tls.crt", ROOT_PEM, "tls.key", PRIVATE_KEY_MARKER
        )
    },
}


def run_ca(*argv: str, **overrides) -> str:
    """`dfe-ops ca` against one cluster reading; stdout and stderr together."""
    fixture = dict(BASE_FIXTURE)
    fixture.update(overrides)
    with tempfile.TemporaryDirectory() as tmp:
        bindir = Path(tmp)
        kubectl = bindir / "kubectl"
        kubectl.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
        kubectl.chmod(0o755)
        fixture_file = bindir / "fixture.json"
        fixture_file.write_text(json.dumps(fixture), encoding="utf-8", newline="\n")

        env = dict(os.environ)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["FAKE_KUBECTL_FIXTURE"] = str(fixture_file)
        env.pop("KUBECONFIG", None)
        out = subprocess.run(
            [sys.executable, str(DFE_OPS), "ca", *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
        return out.stdout + out.stderr


PERSIST_ON = "tls.internalCA.persist.enabled=true"


def persist_pair(*sets: str) -> tuple[dict, dict]:
    """The PushSecret and the restore ExternalSecret for one set of values."""
    docs = render(PERSIST_ON, *sets)
    push = by_kind(docs, "PushSecret")[0]
    restore = next(
        d for d in by_kind(docs, "ExternalSecret")
        if d["metadata"]["name"] == "dfe-internal-ca-restore"
    )
    return push, restore


def test_self_signed_mode_renders_both_halves_against_the_same_entries() -> None:
    """Save and restore must name the SAME store entries, or a rebuild mints again."""
    docs = render(PERSIST_ON)
    pushes = by_kind(docs, "PushSecret")
    restores = [
        d for d in by_kind(docs, "ExternalSecret")
        if d["metadata"]["name"] == "dfe-internal-ca-restore"
    ]
    expect("the PushSecret that saves the root renders", len(pushes) == 1, f"{len(pushes)}")
    expect("the ExternalSecret that restores it renders", len(restores) == 1, f"{len(restores)}")
    if not (pushes and restores):
        return
    push, restore = pushes[0], restores[0]
    push_refs = {
        (d["match"]["remoteRef"]["remoteKey"], d["match"]["remoteRef"]["property"])
        for d in push["spec"]["data"]
    }
    restore_refs = {(d["remoteRef"]["key"], d["remoteRef"]["property"]) for d in restore["spec"]["data"]}
    expect(
        "both name the two entries derived from {project}/{env}/pki/internal-ca",
        push_refs == restore_refs == {(CERT_ENTRY, "tls_crt"), (KEY_ENTRY, "tls_key")},
        f"push={push_refs} restore={restore_refs}",
    )
    expect(
        "and the restore still reads the single entry an older release wrote",
        [d.get("extract", {}).get("key") for d in restore["spec"].get("dataFrom", [])] == [STORE_PATH],
        f"{restore['spec'].get('dataFrom')}",
    )
    expect(
        "the restore only acts while the Secret is absent",
        restore["spec"]["refreshPolicy"] == "CreatedOnce",
        f"{restore['spec'].get('refreshPolicy')}",
    )
    expect(
        "the restore does not own the Secret cert-manager takes over",
        restore["spec"]["target"]["creationPolicy"] == "Orphan",
        f"{restore['spec']['target'].get('creationPolicy')}",
    )
    expect(
        "the restore writes a TLS Secret cert-manager can adopt",
        restore["spec"]["target"]["name"] == "dfe-internal-ca-tls"
        and restore["spec"]["target"]["template"]["type"] == "kubernetes.io/tls",
        f"{restore['spec']['target']}",
    )
    expect(
        "the push never overwrites a stored root",
        push["spec"]["updatePolicy"] == "IfNotExists",
        f"{push['spec'].get('updatePolicy')}",
    )
    expect(
        "and the stored root outlives a teardown",
        push["spec"]["deletionPolicy"] == "None",
        f"{push['spec'].get('deletionPolicy')}",
    )


# ESO v2.11.0 pushSecretEntry asks SecretExists once per data entry, before that
# entry's own push, so the providers' two answers are the whole model.
def aws_exists(store: dict[str, dict[str, str]], key: str, _prop: str) -> bool:
    """providers/v1/aws/secretsmanager SecretExists: GetSecretValue on the key alone."""
    return key in store


def vault_exists(store: dict[str, dict[str, str]], key: str, prop: str) -> bool:
    """providers/v1/vault SecretExists: reads the key, then looks for the property."""
    return prop in store.get(key, {})


def push_if_not_exists(entries: list[dict], exists, source: dict[str, str]) -> dict[str, dict[str, str]]:
    """What an empty store holds after one IfNotExists reconcile of these entries."""
    store: dict[str, dict[str, str]] = {}
    for entry in entries:
        ref = entry["match"]["remoteRef"]
        if exists(store, ref["remoteKey"], ref["property"]):
            continue
        store.setdefault(ref["remoteKey"], {})[ref["property"]] = source[entry["match"]["secretKey"]]
    return store


SOURCE = {"tls.crt": "MINTED-CRT", "tls.key": "MINTED-KEY"}

# The PushSecret data the single-entry layout rendered: both properties on one key.
SHARED_ENTRY_PUSH = [
    {"match": {"secretKey": "tls.crt", "remoteRef": {"remoteKey": STORE_PATH, "property": "tls_crt"}}},
    {"match": {"secretKey": "tls.key", "remoteRef": {"remoteKey": STORE_PATH, "property": "tls_key"}}},
]


def test_each_half_of_the_root_gets_its_own_store_entry() -> None:
    """Two data entries on one key let the first one's create answer the second's existence check."""
    push, _restore = persist_pair()
    keys = [d["match"]["remoteRef"]["remoteKey"] for d in push["spec"]["data"]]
    expect("two entries, one per half", sorted(keys) == sorted({CERT_ENTRY, KEY_ENTRY}), f"{keys}")


def test_both_providers_store_both_halves() -> None:
    """The AWS provider checks the key, Vault the property; each must end up holding the pair."""
    push, _restore = persist_pair()
    want = {CERT_ENTRY: {"tls_crt": "MINTED-CRT"}, KEY_ENTRY: {"tls_key": "MINTED-KEY"}}
    for name, exists in (("aws", aws_exists), ("vault", vault_exists)):
        got = push_if_not_exists(push["spec"]["data"], exists, SOURCE)
        expect(f"{name} stores the cert and the key", got == want, f"{got}")


def test_expected_fail_one_shared_entry_loses_the_key_on_aws() -> None:
    """The control: the single-entry layout this replaces, run through the same model."""
    aws = push_if_not_exists(SHARED_ENTRY_PUSH, aws_exists, SOURCE)
    vault = push_if_not_exists(SHARED_ENTRY_PUSH, vault_exists, SOURCE)
    expect("aws stores only the cert", aws == {STORE_PATH: {"tls_crt": "MINTED-CRT"}}, f"{aws}")
    expect(
        "while vault stores both, which is why only aws lost the root",
        vault == {STORE_PATH: {"tls_crt": "MINTED-CRT", "tls_key": "MINTED-KEY"}},
        f"{vault}",
    )


def read_store(restore: dict, store: dict[str, dict[str, str]]) -> dict[str, str]:
    """The restore's data map: a key the store lacks is skipped under deletionPolicy Merge."""
    data: dict[str, str] = {}
    for src in restore["spec"].get("dataFrom", []):
        data.update(store.get(src["extract"]["key"], {}))
    for entry in restore["spec"]["data"]:
        ref = entry["remoteRef"]
        if ref["key"] not in store:
            continue
        if ref["property"] not in store[ref["key"]]:
            # The AWS provider's plain error, which fails the whole read rather than skipping.
            raise LookupError(f"key {ref['property']} does not exist in secret {ref['key']}")
        data[entry["secretKey"]] = store[ref["key"]][ref["property"]]
    return data


def run_restore_template(restore: dict, data: dict[str, str]) -> tuple[bool, object]:
    """The restore's tls.crt and tls.key templates over one read of the store.

    helm runs the Go template engine and Sprig fail that ESO's v2 engine does. The
    template reaches its data through index, so ESO's missingkey=error has no
    field lookup to act on and both engines see a missing key as empty.
    """
    templates = restore["spec"]["target"]["template"]["data"]
    with tempfile.TemporaryDirectory() as tmp:
        chart = Path(tmp) / "restore"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text(
            "apiVersion: v2\nname: restore\nversion: 0.1.0\n", encoding="utf-8", newline="\n"
        )
        body = "".join(f'{{{{- define "{k}" -}}}}{v}{{{{- end -}}}}\n' for k, v in templates.items())
        body += "".join(f'{k}: {{{{ include "{k}" (.Values.data | default dict) | quote }}}}\n' for k in templates)
        (chart / "templates" / "out.yaml").write_text(body, encoding="utf-8", newline="\n")
        values = Path(tmp) / "data.yaml"
        values.write_text(yaml.safe_dump({"data": data}), encoding="utf-8", newline="\n")
        out = subprocess.run(
            ["helm", "template", "restore", str(chart), "-f", str(values)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    if out.returncode != 0:
        return False, out.stderr
    return True, yaml.safe_load(out.stdout)


NEW_PAIR = {CERT_ENTRY: {"tls_crt": "NEW-CRT"}, KEY_ENTRY: {"tls_key": "NEW-KEY"}}
OLD_PAIR = {STORE_PATH: {"tls_crt": "OLD-CRT", "tls_key": "OLD-KEY"}}
OLD_CERT_ONLY = {STORE_PATH: {"tls_crt": "OLD-CRT"}}


def test_the_restore_reads_back_what_either_provider_stored() -> None:
    """Push then restore, end to end through the same model."""
    push, restore = persist_pair()
    for name, exists in (("aws", aws_exists), ("vault", vault_exists)):
        store = push_if_not_exists(push["spec"]["data"], exists, SOURCE)
        ok, got = run_restore_template(restore, read_store(restore, store))
        expect(
            f"{name}: the restored Secret is the minted pair",
            ok and got == {"tls.crt": "MINTED-CRT", "tls.key": "MINTED-KEY"},
            f"{got}",
        )


def test_the_restore_picks_a_complete_pair_and_never_mixes_layouts() -> None:
    """Every store shape a deployment can be in: new, old, both, and half of each."""
    _push, restore = persist_pair()
    cases = (
        ("the two-entry layout", NEW_PAIR, ("NEW-CRT", "NEW-KEY")),
        ("a Vault store an older release wrote", OLD_PAIR, ("OLD-CRT", "OLD-KEY")),
        ("an AWS store holding the old half-written entry too", {**NEW_PAIR, **OLD_CERT_ONLY},
         ("NEW-CRT", "NEW-KEY")),
        ("a new cert alone beside a complete old pair", {CERT_ENTRY: {"tls_crt": "NEW-CRT"}, **OLD_PAIR},
         ("OLD-CRT", "OLD-KEY")),
    )
    for name, store, (crt, key) in cases:
        ok, got = run_restore_template(restore, read_store(restore, store))
        expect(f"{name}: restores {crt}/{key}", ok and got == {"tls.crt": crt, "tls.key": key}, f"{got}")


def test_expected_fail_no_complete_pair_writes_nothing() -> None:
    """Merge would otherwise write an empty TLS Secret that cert-manager then adopts."""
    _push, restore = persist_pair()
    expect(
        "a missing source is skipped rather than failing the read",
        restore["spec"]["target"].get("deletionPolicy") == "Merge",
        f"{restore['spec']['target'].get('deletionPolicy')}",
    )
    for name, store in (("an empty store", {}), ("the old half-written AWS entry alone", OLD_CERT_ONLY),
                        ("a new key alone", {KEY_ENTRY: {"tls_key": "NEW-KEY"}})):
        ok, got = run_restore_template(restore, read_store(restore, store))
        expect(
            f"{name}: the template refuses, so nothing is written",
            not ok and "the store holds no complete internal CA root" in str(got),
            f"{got}",
        )


def test_a_custom_remote_key_moves_every_entry_with_it() -> None:
    """The two entries and the old one all hang off remoteKey, so an override moves all three."""
    push, restore = persist_pair("tls.internalCA.persist.remoteKey=acme/prod/pki/root")
    push_keys = {d["match"]["remoteRef"]["remoteKey"] for d in push["spec"]["data"]}
    expect("the push names both entries under it",
           push_keys == {"acme/prod/pki/root-crt", "acme/prod/pki/root-key"}, f"{push_keys}")
    expect("and the restore reads the old entry at it",
           restore["spec"]["dataFrom"] == [{"extract": {"key": "acme/prod/pki/root"}}],
           f"{restore['spec']['dataFrom']}")


def test_the_store_properties_carry_no_dot() -> None:
    """The Vault provider reads a property as a gjson path, where a dot nests."""
    docs = render(PERSIST_ON)
    push = by_kind(docs, "PushSecret")[0]
    props = {d["match"]["remoteRef"]["property"] for d in push["spec"]["data"]}
    expect(
        "the remote properties are tls_crt and tls_key",
        props == {"tls_crt", "tls_key"},
        f"{props}",
    )


def test_vault_mode_renders_neither_half() -> None:
    """The estate PKI owns its root, so persisting a local one is meaningless."""
    docs = render(
        PERSIST_ON,
        "tls.vault.server=https://bao.example.com:8200",
        "tls.vault.path=pki_tls/sign/example-service",
        "tls.vault.appRole.roleId=00000000-0000-0000-0000-000000000000",
    )
    expect("no PushSecret in vault mode", not by_kind(docs, "PushSecret"), "")
    expect(
        "no restore ExternalSecret in vault mode",
        not [d for d in by_kind(docs, "ExternalSecret")
             if d["metadata"]["name"] == "dfe-internal-ca-restore"],
        "",
    )
    issuers = [d for d in by_kind(docs, "ClusterIssuer") if "vault" in (d.get("spec") or {})]
    expect("and the edge issuer is the Vault-backed one", len(issuers) == 1, f"{len(issuers)}")


def test_vault_mode_is_refused_on_aws() -> None:
    """An aws deployment runs no OpenBao, so its private CA is the in-cluster one."""
    cmd = ["helm", "template", "gw", str(CHART), "-f", str(COMMON_VALUES),
           "--set", "domain=dfe.example.com", "--set", "env=local", "--set", "cloud=aws",
           "--set", "tls.vault.server=https://bao.example.com:8200",
           "--set", "tls.vault.path=pki_tls/sign/example-service",
           "--set", "tls.vault.appRole.roleId=00000000-0000-0000-0000-000000000000"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    expect("the render refuses tls.vault on aws", out.returncode != 0, "it rendered")
    expect("and says why", "runs no OpenBao" in out.stderr, out.stderr[-300:])


def test_persistence_is_off_by_default_so_a_storeless_deploy_stays_healthy() -> None:
    """An ExternalSecret against a store that is not there is Degraded forever.

    That failed the Argo sync of an otherwise working deploy, so the default is
    off and a deployment that HAS a store turns it on.
    """
    docs = render()
    expect("no PushSecret by default", not by_kind(docs, "PushSecret"), "")
    expect(
        "no restore ExternalSecret by default",
        not [d for d in by_kind(docs, "ExternalSecret")
             if d["metadata"]["name"] == "dfe-internal-ca-restore"],
        "",
    )
    expect(
        "and an empty store name renders neither either",
        not by_kind(
            render(PERSIST_ON, "tls.internalCA.persist.secretStoreName="), "PushSecret"
        ),
        "",
    )


def test_ca_prints_the_public_half_and_the_install_lines() -> None:
    """The operator needs the certificate AND the command, on either OS."""
    text = run_ca()
    expect("the root PEM is printed", ROOT_PEM.strip() in text, text[:200])
    expect(
        "the Linux system-store lines are printed",
        "/usr/local/share/ca-certificates/dfe-internal-ca.crt" in text
        and "update-ca-certificates" in text,
        text,
    )
    expect(
        "the Chromium NSS line names the store and the package",
        "certutil -d sql:$HOME/.pki/nssdb -A -t C,, -n dfe-internal-ca" in text
        and "libnss3-tools" in text,
        text,
    )
    expect(
        "the macOS keychain line is printed",
        "security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain" in text,
        text,
    )


def test_ca_never_prints_the_private_key() -> None:
    """The public half is safe in a log; the key is the whole trust anchor."""
    expect("the key material is absent from the export", PRIVATE_KEY_MARKER not in run_ca(), "")
    expect(
        "and absent from the status report",
        PRIVATE_KEY_MARKER not in run_ca("--status"),
        "",
    )


def test_status_says_newly_minted_without_a_restore() -> None:
    """A root nothing saved is one every client re-trusts after the next rebuild."""
    text = run_ca("--status")
    expect("the mode is reported", "issuer mode: self-signed" in text, text)
    expect("the root reads newly minted", "newly minted, and NOT persisted" in text, text)
    expect(
        "and names the value that turns persistence on",
        "tls.internalCA.persist.enabled" in text,
        text,
    )
    expect("the fingerprint is reported", "sha256 Fingerprint=" in text, text)


def test_status_says_restored_when_the_external_secret_synced() -> None:
    """Restored is the verdict that says a rebuild cost nobody a re-trust."""
    text = run_ca(
        "--status",
        restore_externalsecret={
            "status": {"conditions": [{"type": "Ready", "status": "True"}]}
        },
    )
    expect("the root reads restored", "RESTORED from the secret store" in text, text)


def test_status_reports_an_unsynced_restore_as_not_persisted() -> None:
    """An ExternalSecret that cannot reach the store is a root that will not survive."""
    text = run_ca(
        "--status",
        restore_externalsecret={
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "False", "reason": "SecretSyncedError"}
                ]
            }
        },
    )
    expect("the root reads newly minted", "newly minted" in text, text)
    expect("with the store's own reason", "SecretSyncedError" in text, text)


def test_vault_mode_has_nothing_for_a_developer_box_to_install() -> None:
    """Chaining to the estate PKI is the fix that removes the trust step entirely."""
    text = run_ca(
        wildcard_certificate={"spec": {"issuerRef": {"name": "dfe-openbao"}}},
        clusterissuer={"spec": {"vault": {"server": "https://bao.example.com:8200"}}},
    )
    expect("the export refuses and says why", "there is no deployment-specific root" in text, text)
    expect("and prints no certificate", "BEGIN CERTIFICATE" not in text, text)


def test_status_names_the_issuing_ca_in_vault_mode() -> None:
    """The operator's question in estate mode is which CA signed the edge."""
    text = run_ca(
        "--status",
        wildcard_certificate={"spec": {"issuerRef": {"name": "dfe-openbao"}}},
        clusterissuer={"spec": {"vault": {"server": "https://bao.example.com:8200"}}},
        secrets={"dfe-wildcard-tls": secret_doc("ca.crt", ROOT_PEM)},
    )
    expect("the mode is vault", "issuer mode: vault" in text, text)
    # openssl spaces the subject differently across versions, so the CN is the
    # assertion rather than the formatting.
    issuing = next((line for line in text.splitlines() if "issuing CA:" in line), "")
    expect("the issuing CA is named", "dfe-internal-ca" in issuing, text)
    expect("and nothing is asked of the client", "nothing to install" in text, text)


def test_the_root_install_lines_branch_on_the_operating_system() -> None:
    """A Mac has no NSS store and no update-ca-certificates; Linux has no keychain."""
    darwin = "\n".join(dfe_ops._ca_install_lines("/x/ca.crt", "dfe-internal-ca", "Darwin"))
    linux = "\n".join(dfe_ops._ca_install_lines("/x/ca.crt", "dfe-internal-ca", "Linux"))
    both = "\n".join(dfe_ops._ca_install_lines("/x/ca.crt", "dfe-internal-ca"))
    expect(
        "Darwin gets the keychain line and neither Linux step",
        "security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain"
        in darwin
        and "update-ca-certificates" not in darwin
        and "certutil" not in darwin,
        darwin,
    )
    expect(
        "Linux gets the system store and the NSS store, and no keychain line",
        "update-ca-certificates" in linux
        and "certutil -d sql:$HOME/.pki/nssdb -A -t C,," in linux
        and "security add-trusted-cert" not in linux,
        linux,
    )
    expect(
        "and the export with no platform prints both",
        "update-ca-certificates" in both and "security add-trusted-cert" in both,
        both,
    )


def test_the_non_root_step_runs_nothing_linux_only_on_a_mac() -> None:
    """certutil is not on a Mac, and the keychain step needs root.

    The NSS directory is injected, so the verdict does not depend on whether the
    machine running this has ever opened a Chromium-family browser.
    """
    calls: list[list[str]] = []
    original_run, original_which = dfe_ops._run_text, dfe_ops.shutil.which
    dfe_ops._run_text = lambda cmd, env=None: (calls.append(cmd), (0, "", ""))[1]
    dfe_ops.shutil.which = lambda _name: "/usr/bin/certutil"
    try:
        with tempfile.TemporaryDirectory() as nssdb:
            darwin = dfe_ops._ca_nonroot_install(
                Path("/x/ca.crt"), "dfe-internal-ca", "Darwin", Path(nssdb)
            )
            expect("nothing is executed on Darwin", calls == [], f"{calls}")
            expect("and it says why", "system keychain" in darwin[0], f"{darwin}")
            dfe_ops._ca_nonroot_install(
                Path("/x/ca.crt"), "dfe-internal-ca", "Linux", Path(nssdb)
            )
            expect(
                "while Linux runs certutil into the NSS store",
                [c[0] for c in calls] == ["certutil"] and calls[0][2] == f"sql:{nssdb}",
                f"{calls}",
            )
    finally:
        dfe_ops._run_text, dfe_ops.shutil.which = original_run, original_which


def test_install_reads_the_platform_rather_than_assuming_linux() -> None:
    """The branch is chosen by platform.system(), so it holds on either machine."""
    original = dfe_ops.platform.system
    dfe_ops.platform.system = lambda: "Darwin"
    try:
        chosen = "\n".join(
            dfe_ops._ca_install_lines("/x/ca.crt", "n", dfe_ops.platform.system())
        )
    finally:
        dfe_ops.platform.system = original
    expect(
        "a Darwin platform.system selects the keychain branch",
        "security add-trusted-cert" in chosen and "update-ca-certificates" not in chosen,
        chosen,
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
