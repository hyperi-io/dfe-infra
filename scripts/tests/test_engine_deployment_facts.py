#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_engine_deployment_facts.py
#  Purpose:      Prove the engine is TOLD its tier, its ui pin and the app
#                manifest, from the one place each is already known, so the
#                console never guesses.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The engine can only report what the deploy hands it.

`GET /api/v1/system/deployment` answers what this deployment IS -- its tier, the
transports it carries, the versions it runs -- and every one of those is a fact
the chart injects. Two are new here: the profile, which comes from the SAME
cluster-secret annotation that selects argocd/values/profile-<x>.yaml, and the
dfe-ui pin, which Helm cannot read off a sibling chart and so is mirrored into
the engine's values under check_versions_drift.py.

`GET /api/v1/apps` answers the same way. The app manifest is dfe-infra's
apps.yaml, and Helm reads only files inside a chart, so the chart carries the
copy scripts/composition.py renders and mounts it -- which is what makes a new
or changed app a manifest edit rather than an engine release.

    python3 scripts/tests/test_engine_deployment_facts.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENGINE_CHART = REPO_ROOT / "helm" / "charts" / "dfe-engine"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-apps.yaml"
VALUES = REPO_ROOT / "argocd" / "values"

PROFILES = ("slim", "single", "scale", "mesh")

# The cluster-secret annotation that is the SSoT for the tier at render time.
PROFILE_ANNOTATION = "dfe.hyperi.io/profile"

# Where the chart mounts the manifest, and what it points the engine at.
CATALOGUE_CONFIGMAP = "dfe-engine-app-catalogue"
CATALOGUE_FILE = "/etc/dfe-engine/catalogue/apps.yaml"

# One per deployment-supplied auth ConfigMap the init container copies in.
AUTH_CHECKSUMS = ("checksum/oidc-providers", "checksum/auth-groups", "checksum/ca-bundle")


def render(*args: str, chart: Path = ENGINE_CHART) -> str:
    cmd = ["helm", "template", "dfe-engine", str(chart), *args]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for dfe-engine {args}:\n{out.stderr}")
    return out.stdout


def documents(*args: str, chart: Path = ENGINE_CHART) -> list[dict]:
    return [d for d in yaml.safe_load_all(render(*args, chart=chart)) if d]


def pod_template(*args: str, chart: Path = ENGINE_CHART) -> dict:
    deployment = next(
        d
        for d in documents(*args, chart=chart)
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == "dfe-engine"
    )
    return deployment["spec"]["template"]


def engine_env(*args: str) -> dict[str, str | None]:
    container = pod_template(*args)["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container["env"]}


def test_the_profile_reaches_the_engine() -> None:
    for profile in PROFILES:
        env = engine_env("--set", f"profile={profile}")
        expect(
            f"{profile} is passed to the engine",
            env.get("DFE_PROFILE") == profile,
            f"got {env.get('DFE_PROFILE')!r}",
        )


def test_an_unset_profile_renders_no_variable() -> None:
    """Absent beats empty: the engine's default already means unknown."""
    expect(
        "no profile renders no DFE_PROFILE",
        "DFE_PROFILE" not in engine_env(),
        "the variable was rendered with nothing in it",
    )


def test_the_profile_comes_from_the_annotation_that_picks_the_values_file() -> None:
    """Two copies of the tier in one Application would be two things to get wrong."""
    text = APPSET.read_text(encoding="utf-8")
    parameter = re.search(
        r"- name: profile\n\s*value: '\{\{ index \.metadata\.annotations \"([^\"]+)\" \}\}'",
        text,
    )
    expect(
        "the appset passes a profile parameter",
        parameter is not None,
        "layer2-apps.yaml renders no profile parameter",
    )
    if parameter:
        expect(
            "and it reads the same annotation the values file does",
            parameter.group(1) == PROFILE_ANNOTATION,
            f"got {parameter.group(1)!r}",
        )
    expect(
        "which is the annotation profile-<x>.yaml is selected by",
        f'profile-{{{{ index .metadata.annotations "{PROFILE_ANNOTATION}" }}}}.yaml' in text,
        "the values-file selector no longer reads that annotation",
    )


def test_the_ui_pin_reaches_the_engine() -> None:
    env = engine_env()
    expect(
        "the engine is told the ui version",
        bool(env.get("DFE_UI_VERSION")),
        f"got {env.get('DFE_UI_VERSION')!r}",
    )


def test_the_ui_pin_is_the_one_the_ui_chart_deploys() -> None:
    """A stale mirror would have the console report a version nobody is running."""
    ui_chart = (REPO_ROOT / "helm" / "charts" / "dfe-ui" / "Chart.yaml").read_text(
        encoding="utf-8"
    )
    deployed = re.search(r'appVersion:\s*"([^"]+)"', ui_chart)
    expect("the ui chart pins an appVersion", deployed is not None, "no appVersion found")
    if deployed:
        expect(
            "and the engine reports that same version",
            engine_env().get("DFE_UI_VERSION") == deployed.group(1),
            f"engine says {engine_env().get('DFE_UI_VERSION')!r}, ui chart {deployed.group(1)!r}",
        )


def test_the_manifest_reaches_the_engine() -> None:
    """Without the mount the engine answers off the snapshot in its image."""
    for profile in PROFILES:
        args = (
            "-f",
            str(VALUES / "common.yaml"),
            "-f",
            str(VALUES / f"profile-{profile}.yaml"),
        )
        catalogue = next(
            (
                d
                for d in documents(*args)
                if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == CATALOGUE_CONFIGMAP
            ),
            None,
        )
        expect(
            f"{profile} renders the app catalogue",
            catalogue is not None,
            f"no ConfigMap {CATALOGUE_CONFIGMAP}",
        )
        if catalogue:
            expect(
                f"{profile} mounts the manifest this repo declares",
                catalogue["data"]["apps.yaml"]
                == (ENGINE_CHART / "files" / "apps.yaml").read_text(encoding="utf-8"),
                "the rendered catalogue is not the chart's copy of apps.yaml",
            )
        expect(
            f"{profile} points the engine at the mounted file",
            engine_env(*args).get("DFE_APP_CATALOGUE_FILE") == CATALOGUE_FILE,
            f"got {engine_env(*args).get('DFE_APP_CATALOGUE_FILE')!r}",
        )


def test_the_engine_reads_the_manifest_off_that_configmap() -> None:
    """An env var naming a path nothing mounted fails the engine at startup."""
    template = pod_template()
    volume = next(
        (v for v in template["spec"]["volumes"] if v["name"] == "app-catalogue"), None
    )
    expect(
        "the catalogue volume is the rendered ConfigMap",
        volume is not None and volume["configMap"]["name"] == CATALOGUE_CONFIGMAP,
        f"got {volume!r}",
    )
    mount = next(
        (
            m
            for m in template["spec"]["containers"][0]["volumeMounts"]
            if m["name"] == "app-catalogue"
        ),
        None,
    )
    expect(
        "and it is mounted where the env var looks",
        mount is not None and mount["mountPath"] == str(Path(CATALOGUE_FILE).parent),
        f"got {mount!r}",
    )


def test_a_manifest_edit_moves_the_pod_template() -> None:
    """The engine reads the manifest once at startup, so a ConfigMap-only change
    would sit in etcd unread until something else rolled the pod."""
    with tempfile.TemporaryDirectory(prefix="dfe-engine-chart-") as tmp:
        edited = Path(tmp) / "dfe-engine"
        shutil.copytree(ENGINE_CHART, edited)
        manifest = edited / "files" / "apps.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "scale_deployed: true", "scale_deployed: false", 1
            ),
            encoding="utf-8",
        )
        before = pod_template()["metadata"]["annotations"].get("checksum/app-catalogue")
        after = pod_template(chart=edited)["metadata"]["annotations"].get(
            "checksum/app-catalogue"
        )
        expect("the pod template carries a catalogue checksum", bool(before), "no annotation")
        expect(
            "and a manifest edit moves it",
            before != after,
            "the checksum is unchanged, so the edit would never reach a pod",
        )


def test_an_auth_configmap_edit_can_reach_the_engine() -> None:
    """The init container copies the three auth ConfigMaps once at pod start, and
    the umbrella installs no Reloader, so without a checksum an edit sits unread."""
    args = (
        "--set", "authConfig.providersConfigMap=dfe-oidc-providers",
        "--set", "authConfig.groupsConfigMap=dfe-auth-groups",
        "--set", "authConfig.caBundleConfigMap=dfe-ca-bundle",
    )
    annotations = pod_template(*args)["metadata"]["annotations"]
    for key in AUTH_CHECKSUMS:
        expect(
            f"the pod template carries {key}",
            bool(annotations.get(key)),
            f"annotations: {sorted(annotations)}",
        )


def test_an_unset_auth_configmap_renders_no_checksum() -> None:
    """A checksum over a ConfigMap this deployment does not mount is noise."""
    annotations = pod_template()["metadata"]["annotations"]
    for key in AUTH_CHECKSUMS:
        expect(
            f"no {key} when nothing supplies that ConfigMap",
            key not in annotations,
            f"annotations: {sorted(annotations)}",
        )


def test_the_engine_gets_a_boot_budget() -> None:
    """Liveness starts at 10s. First boot seeds every registry and reconciles the
    ClickHouse fence, and k8s suspends liveness while a startupProbe runs."""
    probe = pod_template()["spec"]["containers"][0].get("startupProbe")
    expect("the engine container has a startupProbe", probe is not None, "none rendered")
    if probe:
        expect(
            "and it targets the health port the other probes use",
            probe["httpGet"] == {"path": "/livez", "port": "obs"},
            f"got {probe['httpGet']!r}",
        )
        expect(
            "with a budget longer than the liveness delay",
            probe["failureThreshold"] * probe["periodSeconds"] >= 300,
            f"got {probe['failureThreshold']} * {probe['periodSeconds']}s",
        )


def test_an_external_tls_clickhouse_is_expressible() -> None:
    """The chart hardcoded secure false, so a deployment whose ClickHouse speaks
    TLS could not be rendered at all (#277). A render with no CA stays plaintext;
    the Argo cascade's TLS is test_clickhouse_tls.py's."""
    env = engine_env()
    expect(
        "a render with no CA is plaintext",
        env.get("DFE_CLICKHOUSE_SECURE") == "false",
        f"got {env.get('DFE_CLICKHOUSE_SECURE')!r}",
    )
    expect(
        "on the plaintext port",
        env.get("DFE_CLICKHOUSE_PORT") == "8123",
        f"got {env.get('DFE_CLICKHOUSE_PORT')!r}",
    )
    expect(
        "and a plaintext connection renders no verify dial",
        "DFE_CLICKHOUSE_VERIFY" not in env,
        "there is no certificate to verify on 8123",
    )
    tls = engine_env(
        "--set", "clickhouse.mode=external",
        "--set", "clickhouse.tls.enabled=true",
        "--set", "clickhouse.tls.verify=false",
    )
    expect(
        "a TLS ClickHouse renders secure",
        tls.get("DFE_CLICKHOUSE_SECURE") == "true",
        f"got {tls.get('DFE_CLICKHOUSE_SECURE')!r}",
    )
    expect(
        "on the TLS port",
        tls.get("DFE_CLICKHOUSE_PORT") == "8443",
        f"got {tls.get('DFE_CLICKHOUSE_PORT')!r}",
    )
    expect(
        "and a self-signed one renders verify false rather than dropping it",
        tls.get("DFE_CLICKHOUSE_VERIFY") == "false",
        f"got {tls.get('DFE_CLICKHOUSE_VERIFY')!r}",
    )


def test_the_token_lifetime_is_a_dial() -> None:
    expect(
        "unset leaves the engine's own default",
        "DFE_API_JWT_EXPIRE_MINUTES" not in engine_env(),
        "the variable was rendered with nothing behind it",
    )
    expect(
        "and a deployment can shorten it",
        engine_env("--set", "api.jwtExpireMinutes=15").get("DFE_API_JWT_EXPIRE_MINUTES") == "15",
        "the dial did not reach the container",
    )


# The engine's optional auth and session settings (dfe-infra#475): each values key
# and the variable the engine reads it from.
AUTH_DIALS = {
    "api.maxSessionMinutes": "DFE_API_MAX_SESSION_MINUTES",
    "api.docsEnabled": "DFE_API_DOCS_ENABLED",
    "auth.proxyProvider": "DFE_AUTH_PROXY_PROVIDER",
    "auth.apiKeyDefaultTtlDays": "DFE_AUTH_API_KEY_DEFAULT_TTL_DAYS",
    "auth.loginThrottle.enabled": "DFE_AUTH_LOGIN_THROTTLE_ENABLED",
    "auth.loginThrottle.usernameFailures": "DFE_AUTH_LOGIN_THROTTLE_USERNAME_FAILURES",
    "auth.loginThrottle.clientFailures": "DFE_AUTH_LOGIN_THROTTLE_CLIENT_FAILURES",
    "auth.loginThrottle.maxDelaySeconds": "DFE_AUTH_LOGIN_THROTTLE_MAX_DELAY_SECONDS",
}


def refusal(*args: str) -> str:
    """The render's stderr, or empty when it rendered."""
    out = subprocess.run(
        ["helm", "template", "dfe-engine", str(ENGINE_CHART), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return out.stderr if out.returncode != 0 else ""


def test_an_unset_auth_dial_renders_no_variable() -> None:
    """Absent, the engine takes its own default; an empty variable is not absent."""
    for args in ((), ("-f", str(VALUES / "common.yaml"))):
        env = engine_env(*args)
        for key, name in AUTH_DIALS.items():
            got = env.get(name)
            expect(f"unset {key} renders no {name} {args}", name not in env, f"got {got!r}")


def test_each_auth_dial_reaches_the_engine() -> None:
    sets = {
        "api.maxSessionMinutes": "240",
        "api.docsEnabled": "false",
        "auth.proxyProvider": "dex",
        "auth.apiKeyDefaultTtlDays": "30",
        "auth.loginThrottle.enabled": "false",
        "auth.loginThrottle.usernameFailures": "3",
        "auth.loginThrottle.clientFailures": "10",
        "auth.loginThrottle.maxDelaySeconds": "600",
    }
    args = [arg for key, value in sets.items() for arg in ("--set", f"{key}={value}")]
    env = engine_env(*args)
    for key, value in sets.items():
        name = AUTH_DIALS[key]
        expect(f"{key}={value} renders {name}", env.get(name) == value, f"got {env.get(name)!r}")


def test_a_zero_key_lifetime_is_rendered_not_dropped() -> None:
    """0 is a key with no expiry, the one value a truthiness test would lose."""
    name = AUTH_DIALS["auth.apiKeyDefaultTtlDays"]
    got = engine_env("--set", "auth.apiKeyDefaultTtlDays=0").get(name)
    expect("apiKeyDefaultTtlDays=0 reaches the engine as 0", got == "0", f"got {got!r}")


def test_a_count_from_a_values_file_renders_whole() -> None:
    """A values-file number is a float64, which renders 1e+06 bare."""
    with tempfile.TemporaryDirectory(prefix="dfe-engine-auth-dials-") as tmp:
        overlay = Path(tmp) / "dials.yaml"
        overlay.write_text(
            "api:\n  maxSessionMinutes: 1000000\n"
            "auth:\n  loginThrottle:\n    maxDelaySeconds: 900\n    enabled: true\n",
            encoding="utf-8", newline="\n",
        )
        env = engine_env("-f", str(overlay))
    expect("maxSessionMinutes renders as whole minutes",
           env.get("DFE_API_MAX_SESSION_MINUTES") == "1000000",
           f"got {env.get('DFE_API_MAX_SESSION_MINUTES')!r}")
    expect("maxDelaySeconds renders as whole seconds",
           env.get("DFE_AUTH_LOGIN_THROTTLE_MAX_DELAY_SECONDS") == "900",
           f"got {env.get('DFE_AUTH_LOGIN_THROTTLE_MAX_DELAY_SECONDS')!r}")
    expect("a bare true renders true", env.get("DFE_AUTH_LOGIN_THROTTLE_ENABLED") == "true",
           f"got {env.get('DFE_AUTH_LOGIN_THROTTLE_ENABLED')!r}")


def test_a_value_the_engine_cannot_take_fails_the_render() -> None:
    """The engine reads any word but true, 1 and yes as false, and stops at
    startup on a count under its floor, so each is refused by name instead."""
    cases = (
        ("api.docsEnabled=yes", "api.docsEnabled"),
        ("auth.loginThrottle.enabled=off", "auth.loginThrottle.enabled"),
        ("auth.loginThrottle.usernameFailures=0", "auth.loginThrottle.usernameFailures"),
        ("auth.loginThrottle.clientFailures=0", "auth.loginThrottle.clientFailures"),
        ("auth.loginThrottle.maxDelaySeconds=0", "auth.loginThrottle.maxDelaySeconds"),
        ("api.maxSessionMinutes=0", "api.maxSessionMinutes"),
        ("api.maxSessionMinutes=1.5", "api.maxSessionMinutes"),
        ("auth.apiKeyDefaultTtlDays=-1", "auth.apiKeyDefaultTtlDays"),
        ("auth.apiKeyDefaultTtlDays=90d", "auth.apiKeyDefaultTtlDays"),
    )
    for value, key in cases:
        err = refusal("--set", value)
        expect(f"{value} is refused naming {key}", key in err, f"stderr={err[-300:]!r}")


FORWARDED_ENV = "DFE_API_FORWARDED_ALLOW_IPS"


def test_the_proxy_hops_default_to_the_deployments_pod_range() -> None:
    """Behind the gateway every request arrives from a pod. Unlisted, the engine
    builds http OIDC callbacks and audits the gateway's address for everyone."""
    common = yaml.safe_load((VALUES / "common.yaml").read_text(encoding="utf-8"))
    pod_cidr = common["networkModel"]["podCIDR"]
    expect("common.yaml declares a pod range", bool(pod_cidr), f"got {pod_cidr!r}")
    cascade = ("-f", str(VALUES / "common.yaml"))
    got = engine_env(*cascade).get(FORWARDED_ENV)
    expect("the Argo cascade trusts that range", got == pod_cidr, f"got {got!r}")
    moved = engine_env(*cascade, "--set", "networkModel.podCIDR=10.42.0.0/16")
    expect("and follows it when a deployment moves it",
           moved.get(FORWARDED_ENV) == "10.42.0.0/16", f"got {moved.get(FORWARDED_ENV)!r}")


def test_a_named_hop_list_beats_the_pod_range() -> None:
    hops = "10.42.7.0/24,10.42.8.0/24"
    with tempfile.TemporaryDirectory(prefix="dfe-engine-hops-") as tmp:
        overlay = Path(tmp) / "hops.yaml"
        overlay.write_text(f'api:\n  forwardedAllowIps: "{hops}"\n', encoding="utf-8", newline="\n")
        got = engine_env("-f", str(VALUES / "common.yaml"), "-f", str(overlay)).get(FORWARDED_ENV)
    expect("api.forwardedAllowIps wins", got == hops, f"got {got!r}")


def test_a_render_with_no_pod_range_keeps_the_engines_default() -> None:
    """A standalone render knows no pod range, and the engine's loopback default
    fails closed there."""
    got = engine_env().get(FORWARDED_ENV)
    expect("no pod range renders no hop list", got is None, f"got {got!r}")


TOPIC_SIZE_ENV = "DFE_KAFKA_TOPIC_MAX_MESSAGE_BYTES"


def test_the_topic_size_reaches_the_engine_only_when_set() -> None:
    """A managed broker refuses a record above its provider default, so the
    engine's own topics take the size the landing topics were created at."""
    expect(
        "unset leaves the engine's own default",
        TOPIC_SIZE_ENV not in engine_env(),
        "the variable was rendered with nothing behind it",
    )
    # layer2-apps.yaml hands the annotation over as a quoted string.
    got = engine_env("--set-string", "kafka.messageMaxBytes=8388608").get(TOPIC_SIZE_ENV)
    expect("the appset's string reaches the container", got == "8388608", f"got {got!r}")
    # A values file reads a number as a float64, which renders as 1.6777216e+07 bare.
    with tempfile.TemporaryDirectory(prefix="dfe-engine-topic-size-") as tmp:
        overlay = Path(tmp) / "size.yaml"
        overlay.write_text("kafka:\n  messageMaxBytes: 16777216\n", encoding="utf-8", newline="\n")
        got = engine_env("-f", str(overlay)).get(TOPIC_SIZE_ENV)
    expect("a number from a values file renders as whole bytes", got == "16777216", f"got {got!r}")


def test_a_topic_size_that_is_not_bytes_fails_the_render() -> None:
    """A unit suffix would reach the engine as a setting it cannot parse."""
    out = subprocess.run(
        ["helm", "template", "dfe-engine", str(ENGINE_CHART),
         "--set-string", "kafka.messageMaxBytes=16MiB"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    expect(
        "the render refuses it by name",
        out.returncode != 0 and "kafka.messageMaxBytes" in out.stderr,
        f"rc={out.returncode} stderr={out.stderr[-300:]!r}",
    )


def test_every_profile_still_renders_with_the_real_overlays() -> None:
    for profile in PROFILES:
        env = engine_env(
            "-f",
            str(VALUES / "common.yaml"),
            "-f",
            str(VALUES / f"profile-{profile}.yaml"),
            "--set",
            f"profile={profile}",
        )
        expect(
            f"{profile} renders the engine with its tier",
            env.get("DFE_PROFILE") == profile,
            f"got {env.get('DFE_PROFILE')!r}",
        )
        expect(
            f"{profile} still carries the transport facts",
            env.get("DFE_TRANSPORT_DEFAULT") in ("bus", "direct"),
            f"got {env.get('DFE_TRANSPORT_DEFAULT')!r}",
        )


def main() -> int:
    with standalone():
        test_the_profile_reaches_the_engine()
        test_an_unset_profile_renders_no_variable()
        test_the_profile_comes_from_the_annotation_that_picks_the_values_file()
        test_the_ui_pin_reaches_the_engine()
        test_the_ui_pin_is_the_one_the_ui_chart_deploys()
        test_the_manifest_reaches_the_engine()
        test_the_engine_reads_the_manifest_off_that_configmap()
        test_a_manifest_edit_moves_the_pod_template()
        test_an_auth_configmap_edit_can_reach_the_engine()
        test_an_unset_auth_configmap_renders_no_checksum()
        test_the_engine_gets_a_boot_budget()
        test_an_external_tls_clickhouse_is_expressible()
        test_the_token_lifetime_is_a_dial()
        test_an_unset_auth_dial_renders_no_variable()
        test_each_auth_dial_reaches_the_engine()
        test_a_zero_key_lifetime_is_rendered_not_dropped()
        test_a_count_from_a_values_file_renders_whole()
        test_a_value_the_engine_cannot_take_fails_the_render()
        test_the_proxy_hops_default_to_the_deployments_pod_range()
        test_a_named_hop_list_beats_the_pod_range()
        test_a_render_with_no_pod_range_keeps_the_engines_default()
        test_the_topic_size_reaches_the_engine_only_when_set()
        test_a_topic_size_that_is_not_bytes_fails_the_render()
        test_every_profile_still_renders_with_the_real_overlays()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
