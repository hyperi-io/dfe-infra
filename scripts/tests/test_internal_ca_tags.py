#  Project:      dfe-infra
#  File:         scripts/tests/test_internal_ca_tags.py
#  Purpose:      Prove the cloud root's tag set travels from tofu to the internal
#                CA's PushSecret on both paths that apply it, and that a
#                deployment with no tags renders the PushSecret it did before.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The internal CA's store entries carry the cloud root's tags.

default_tags reaches what tofu creates and nothing ESO creates, and an account
guardrail can refuse a CreateSecret without the tags it requires. The aws root
emits DFE_RESOURCE_TAGS (its local.tags), bootstrap.sh hands it to its own
pre-Argo render of the CA pair and writes it onto the cluster secret, and
layer2-edge.yaml hands the annotation to the gateway chart. The chart puts the
map on each PushSecret entry as the AWS provider's PushSecretMetadata tags.

The appset's values block is rendered by helm, which runs the same Go template
engine and Sprig functions Argo CD's goTemplate does.

    python3 -m pytest scripts/tests/test_internal_ca_tags.py -q
"""

import json
import subprocess
from pathlib import Path

import yaml

from _charts import chart_dir

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-edge.yaml"
AWS_OUTPUTS = REPO_ROOT / "terraform" / "environments" / "aws" / "outputs.tf"
COMMON_VALUES = REPO_ROOT / "argocd" / "values" / "common.yaml"

KEY = "dfe.hyperi.io/resource_tags"
TAGS = {
    "service-name": "dfe",
    "owner": "platform",
    "lifecycle": "ephemeral",
    "dfe-e2e": "run-1",
    "expires-at": "2026-10-08T12:00:00Z",
}
TAGS_JSON = json.dumps(TAGS, separators=(",", ":"))


def _run(script: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def _bootstrap_lines() -> list[str]:
    return BOOTSTRAP.read_text(encoding="utf-8").splitlines()


def _annotation_block() -> str:
    """bootstrap.sh's own composition of the annotation, run on its own rather than copied."""
    lines = _bootstrap_lines()
    start = lines.index('export DFE_RESOURCE_TAGS="${DFE_RESOURCE_TAGS:-}"')
    end = next(i for i, line in enumerate(lines) if line.startswith("export DFE_RESOURCE_TAGS_ANNOTATION="))
    return "\n".join(lines[start : end + 1])


def _ca_tag_args_block() -> str:
    """The two lines that build the --set-json argument for bootstrap's CA render."""
    lines = [line.strip() for line in _bootstrap_lines()]
    start = lines.index("ca_tag_args=()")
    return "\n".join(lines[start : start + 2])


def _gateway(*sets: str, set_json: tuple[str, ...] = ()) -> list[dict]:
    cmd = ["helm", "template", "gw", str(chart_dir("envoy-gateway-config")), "-f", str(COMMON_VALUES),
           "--set", "domain=dfe.example.com", "--set", "env=local",
           "--set", "tls.internalCA.persist.enabled=true",
           "--show-only", "templates/internal-ca-persist.yaml"]
    for item in sets:
        cmd += ["--set", item]
    cmd += list(set_json)
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _push_entries(docs: list[dict]) -> list[dict]:
    return next(d for d in docs if d["kind"] == "PushSecret")["spec"]["data"]


# --- the aws root and bootstrap.sh -------------------------------------------------


def test_the_aws_root_emits_its_whole_tag_set() -> None:
    """local.tags, not var.tags: a CreateSecret without the run's tags is the one a guardrail refuses."""
    body = AWS_OUTPUTS.read_text(encoding="utf-8")
    start = body.index('output "DFE_RESOURCE_TAGS"')
    block = body[start : body.index("\n}\n", start)]
    assert "jsonencode(local.tags)" in block, block


def test_bootstrap_writes_the_annotation() -> None:
    done = _run(_annotation_block() + '\nprintf "%s\\n" "$DFE_RESOURCE_TAGS_ANNOTATION"', DFE_RESOURCE_TAGS=TAGS_JSON)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [f"{KEY}: '{TAGS_JSON}'"]


def test_no_tags_write_no_annotation() -> None:
    done = _run(_annotation_block() + '\nprintf "%s\\n" "$DFE_RESOURCE_TAGS_ANNOTATION"')
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [""]


def test_the_cluster_secret_carries_the_tags() -> None:
    done = _run(_annotation_block() + '\nprintf "%s\\n" "$DFE_RESOURCE_TAGS_ANNOTATION"',
                DFE_RESOURCE_TAGS=json.dumps({**TAGS, "owner": "platform's"}))
    rendered = subprocess.run(
        ["envsubst"],
        input=CLUSTER_SECRET.read_text(encoding="utf-8"),
        env={"PATH": "/usr/bin:/bin", "DFE_RESOURCE_TAGS_ANNOTATION": done.stdout.strip()},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    annotations = yaml.safe_load(rendered)["metadata"]["annotations"]
    assert json.loads(annotations[KEY]) == {**TAGS, "owner": "platform's"}


def test_bootstrap_hands_the_tags_to_its_own_ca_render() -> None:
    """bootstrap.sh applies the PushSecret ahead of Argo, so its render is the one that creates the entries."""
    done = _run(_ca_tag_args_block() + '\nprintf "%s\\n" "${ca_tag_args[@]}"', DFE_RESOURCE_TAGS=TAGS_JSON)
    assert done.returncode == 0, done.stderr
    args = tuple(done.stdout.splitlines())
    assert args == ("--set-json", f"tls.internalCA.persist.tags={TAGS_JSON}")
    assert '"${ca_tag_args[@]}"' in BOOTSTRAP.read_text(encoding="utf-8")
    for entry in _push_entries(_gateway(set_json=args)):
        assert entry["metadata"]["spec"]["tags"] == TAGS, entry


def test_bootstrap_adds_no_argument_without_tags() -> None:
    done = _run(_ca_tag_args_block() + '\nprintf "%s\\n" "${#ca_tag_args[@]}"', DFE_RESOURCE_TAGS="")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "0"


# --- layer2-edge.yaml ---------------------------------------------------------------


def _render_values_block(tmp_path: Path, annotations: dict[str, str]) -> dict:
    """The gateway Application's values block for a cluster carrying these annotations."""
    gateway = next(yaml.safe_load_all(APPSET.read_text(encoding="utf-8")))
    block = gateway["spec"]["template"]["spec"]["sources"][0]["helm"]["values"]
    chart = tmp_path / "appset-values"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: appset-values\nversion: 0.1.0\n", encoding="utf-8")
    (chart / "templates" / "values.yaml").write_text(
        "{{- with .Values.cluster }}\n" + block + "\n{{- end }}\n", encoding="utf-8"
    )
    cluster = tmp_path / "cluster.yaml"
    cluster.write_text(yaml.safe_dump({"cluster": {"metadata": {"annotations": annotations}}}), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "appset-values", str(chart), "-f", str(cluster)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert out.returncode == 0, out.stderr
    return yaml.safe_load(out.stdout) or {}


def test_the_appset_hands_the_gateway_the_tags(tmp_path: Path) -> None:
    values = _render_values_block(tmp_path, {KEY: TAGS_JSON})
    assert values["tls"] == {"internalCA": {"persist": {"tags": TAGS}}}


def test_a_cluster_without_the_annotation_gets_no_tls_key(tmp_path: Path) -> None:
    """An inline value beats the deploy-repo overlay, so an empty one would erase a deployer's own tags."""
    assert "tls" not in _render_values_block(tmp_path, {})


# --- the gateway chart --------------------------------------------------------------


def test_the_push_puts_the_tags_on_both_store_entries() -> None:
    entries = _push_entries(_gateway(set_json=("--set-json", f"tls.internalCA.persist.tags={TAGS_JSON}")))
    assert len(entries) == 2
    for entry in entries:
        meta = entry["metadata"]
        assert meta["apiVersion"] == "kubernetes.external-secrets.io/v1alpha1", meta
        assert meta["kind"] == "PushSecretMetadata", meta
        assert meta["spec"] == {"tags": TAGS}, meta


def test_no_tags_render_no_metadata_block() -> None:
    """A deployment without tags renders entries with no metadata, as it did before tags existed."""
    for entry in _push_entries(_gateway()):
        assert "metadata" not in entry, entry


def test_a_managed_by_tag_is_dropped_rather_than_failing_the_push() -> None:
    """ESO's AWS provider sets managed-by itself and fails a push whose tags already name it."""
    tags = {**TAGS, "managed-by": "opentofu"}
    for entry in _push_entries(_gateway(set_json=("--set-json", f"tls.internalCA.persist.tags={json.dumps(tags)}"))):
        assert "managed-by" not in entry["metadata"]["spec"]["tags"], entry
        assert entry["metadata"]["spec"]["tags"] == TAGS, entry


def test_a_number_in_the_tags_reaches_the_provider_as_a_string() -> None:
    """The provider reads tags as map[string]string, and a YAML number fails that parse."""
    entries = _push_entries(_gateway(set_json=("--set-json", 'tls.internalCA.persist.tags={"cost-center":1234}')))
    for entry in entries:
        assert entry["metadata"]["spec"]["tags"] == {"cost-center": "1234"}, entry
