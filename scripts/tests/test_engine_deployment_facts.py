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
the deployment injects. The profile comes from the SAME cluster-secret annotation
that selects argocd/values/profile-<x>.yaml, and the dfe-ui pin, which Helm cannot
read off a sibling chart, is mirrored into dfe-extras' values under
check_versions_drift.py.

`GET /api/v1/apps` answers the same way. The app manifest is dfe-infra's
apps.yaml, and Helm reads only files inside a chart, so dfe-extras carries the
copy scripts/composition.py renders and the engine mounts it -- which is what
makes a new or changed app a manifest edit rather than an engine release.

Each fact is read off the deployed render: the golden of each cell, the engine's
thin chart beside dfe-extras, which writes the engine's literal env into the
dfe-engine-env ConfigMap the pod reads. A dial a deployer turns is rendered as
its instance values or its deploy repo's infra/common.yaml; a cluster fact as a
cluster-secret annotation.

    python3 scripts/tests/test_engine_deployment_facts.py

Needs `helm` on PATH; the cases that render the engine pod need the scalo-service
library (_weave.library). No test runner, matching the other checks here.
"""

import re
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary
from _gate import CLOUDS, PROFILES, effective_env, golden
from _weave import REPO_ROOT, render_app, render_extras, weave

APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-apps.yaml"
VALUES = REPO_ROOT / "argocd" / "values"
EXTRAS = REPO_ROOT / "helm" / "charts" / "dfe-extras"

ENGINE = "dfe-engine"
WORKLOAD = f"Deployment/{ENGINE}"
CELLS = [(p, k) for p in PROFILES for k in CLOUDS]
BUS_PROFILES = ("single", "scale")

# The cluster-secret annotation that is the SSoT for the tier at render time.
PROFILE_ANNOTATION = "dfe.hyperi.io/profile"

# Where the engine mounts the manifest, and what it points the engine at.
CATALOGUE_CONFIGMAP = "dfe-engine-app-catalogue"
CATALOGUE_FILE = "/etc/dfe-engine/catalogue/apps.yaml"

# The Reloader annotations that roll the engine when a ConfigMap it reads changes.
RELOAD_AUTO = "reloader.stakater.com/auto"
RELOAD_CONFIGMAPS = "configmap.reloader.stakater.com/reload"

# authConfig key -> the pod volume the auth seed init container copies it from.
AUTH_VOLUMES = {
    "providersConfigMap": "oidc-providers",
    "groupsConfigMap": "auth-groups",
    "caBundleConfigMap": "ca-bundle",
}

# A managed broker's cluster secret, as bootstrap.sh writes it: RFC 2606 names only.
MANAGED_BROKER = {
    "dfe.hyperi.io/kafka_mode": "external",
    "dfe.hyperi.io/kafka_bootstrap": "b-1.kafka.example.com:9096",
    "dfe.hyperi.io/kafka_security_protocol": "SASL_SSL",
}


def nested(dotted: dict[str, object]) -> dict:
    """Instance values from `a.b.c: value` pairs."""
    out: dict = {}
    for path, value in dotted.items():
        *parents, leaf = path.split(".")
        node = out
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = value
    return out


def deployment(docs: list[dict], name: str = ENGINE) -> dict:
    return next(
        d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == name
    )


def pod_spec(docs: list[dict], name: str = ENGINE) -> dict:
    return deployment(docs, name)["spec"]["template"]["spec"]


def engine_env(docs: list[dict]) -> dict[str, object]:
    """The engine container's env as the kubelet hands it."""
    return effective_env(docs, WORKLOAD)


def literal_env(profile: str = "slim", **options: object) -> dict[str, str]:
    """The literal env dfe-extras hands the engine, from its dfe-engine-env ConfigMap."""
    docs = render_extras(ENGINE, profile, "local", **options)
    return next(
        d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == f"{ENGINE}-env"
    )["data"]


def refusal(profile: str = "slim", **options: object) -> str:
    """The render's refusal, or empty when it rendered."""
    try:
        render_extras(ENGINE, profile, "local", **options)
    except weave().WeaveError as exc:
        return str(exc)
    return ""


def test_the_profile_reaches_the_engine() -> None:
    for profile, cloud in CELLS:
        env = engine_env(golden(ENGINE, profile, cloud))
        expect(
            f"{profile}/{cloud} is passed to the engine",
            env.get("DFE_PROFILE") == profile,
            f"got {env.get('DFE_PROFILE')!r}",
        )


def test_an_unset_profile_renders_no_variable() -> None:
    """Absent beats empty: the engine's default already means unknown."""
    env = engine_env(render_app(ENGINE, "", "local"))
    expect(
        "a cluster secret naming no profile renders no DFE_PROFILE",
        "DFE_PROFILE" not in env,
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
    for profile, cloud in CELLS:
        env = engine_env(golden(ENGINE, profile, cloud))
        expect(
            f"{profile}/{cloud}: the engine is told the ui version",
            bool(env.get("DFE_UI_VERSION")),
            f"got {env.get('DFE_UI_VERSION')!r}",
        )


def test_the_ui_pin_is_the_one_the_ui_chart_deploys() -> None:
    """A stale mirror would have the console report a version nobody is running."""
    deployed, _ = weave().pinned_image(REPO_ROOT, "dfe-ui")
    told = literal_env().get("DFE_UI_VERSION")
    expect(
        "the engine reports the tag the dfe-ui thin chart runs",
        told == deployed,
        f"engine says {told!r}, the dfe-ui chart runs {deployed!r}",
    )


def test_the_manifest_reaches_the_engine() -> None:
    """Without the mount the engine answers off the snapshot in its image."""
    manifest = (EXTRAS / "files" / "apps.yaml").read_text(encoding="utf-8")
    for profile in PROFILES:
        docs = golden(ENGINE, profile, "local")
        catalogue = next(
            (
                d
                for d in docs
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
                catalogue["data"]["apps.yaml"] == manifest,
                "the rendered catalogue is not dfe-extras' copy of apps.yaml",
            )
        expect(
            f"{profile} points the engine at the mounted file",
            engine_env(docs).get("DFE_APP_CATALOGUE_FILE") == CATALOGUE_FILE,
            f"got {engine_env(docs).get('DFE_APP_CATALOGUE_FILE')!r}",
        )


def test_the_engine_reads_the_manifest_off_that_configmap() -> None:
    """An env var naming a path nothing mounted fails the engine at startup."""
    spec = pod_spec(golden(ENGINE, "slim", "local"))
    volume = next((v for v in spec["volumes"] if v["name"] == "app-catalogue"), None)
    expect(
        "the catalogue volume is the rendered ConfigMap",
        volume is not None and volume.get("configMap", {}).get("name") == CATALOGUE_CONFIGMAP,
        f"got {volume!r}",
    )
    mount = next(
        (m for m in spec["containers"][0]["volumeMounts"] if m["name"] == "app-catalogue"), None
    )
    expect(
        "and it is mounted where the env var looks",
        mount is not None and mount["mountPath"] == str(Path(CATALOGUE_FILE).parent),
        f"got {mount!r}",
    )


def test_a_manifest_edit_rolls_the_engine() -> None:
    """The engine reads the manifest once at startup, so a ConfigMap-only change
    would sit in etcd unread until something else rolled the pod. Reloader rolls
    the Deployment when a ConfigMap its reload annotation names changes."""
    for profile, cloud in CELLS:
        metadata = deployment(golden(ENGINE, profile, cloud))["metadata"]
        annotations = metadata.get("annotations") or {}
        watched = (annotations.get(RELOAD_CONFIGMAPS) or "").split(",")
        expect(
            f"{profile}/{cloud}: Reloader rolls the engine when the catalogue changes",
            annotations.get(RELOAD_AUTO) == "true" and CATALOGUE_CONFIGMAP in watched,
            f"annotations: {annotations}",
        )


def test_an_auth_configmap_edit_can_reach_the_engine() -> None:
    """The init container copies the three auth ConfigMaps once at pod start, so an
    edit reaches the engine only through a roll. Reloader's auto mode rolls the
    Deployment when a ConfigMap the pod mounts changes, so each must be mounted."""
    names = {key: f"dfe-{volume}" for key, volume in AUTH_VOLUMES.items()}
    docs = render_app(ENGINE, "slim", "local", overlay={"authConfig": names})
    annotations = deployment(docs)["metadata"].get("annotations") or {}
    expect(
        "the engine Deployment is under Reloader's auto mode",
        annotations.get(RELOAD_AUTO) == "true",
        f"annotations: {annotations}",
    )
    volumes = {v["name"]: v for v in pod_spec(docs)["volumes"]}
    for key, volume in AUTH_VOLUMES.items():
        expect(
            f"the pod mounts the ConfigMap authConfig.{key} names",
            volumes.get(volume, {}).get("configMap", {}).get("name") == names[key],
            f"got {volumes.get(volume)!r}",
        )


def test_an_unset_auth_configmap_is_not_mounted() -> None:
    """A deployment that supplies no auth ConfigMap gives the pod nothing to wait on."""
    for profile, cloud in CELLS:
        volumes = {v["name"]: v for v in pod_spec(golden(ENGINE, profile, cloud))["volumes"]}
        for key, volume in AUTH_VOLUMES.items():
            source = volumes.get(volume, {})
            expect(
                f"{profile}/{cloud}: no ConfigMap backs {volume} when authConfig.{key} is unset",
                "configMap" not in source and "emptyDir" in source,
                f"got {source!r}",
            )


def test_the_engine_gets_a_boot_budget() -> None:
    """First boot seeds every registry and reconciles the ClickHouse fence, and k8s
    suspends liveness while a startupProbe runs."""
    container = pod_spec(golden(ENGINE, "slim", "local"))["containers"][0]
    probe = container.get("startupProbe")
    expect("the engine container has a startupProbe", probe is not None, "none rendered")
    if probe:
        expect(
            "and it targets the health port and path the liveness probe uses",
            probe["httpGet"] == container["livenessProbe"]["httpGet"]
            and probe["httpGet"]["path"] == "/livez",
            f"got {probe['httpGet']!r}",
        )
        expect(
            "with a budget longer than the liveness delay",
            probe["failureThreshold"] * probe["periodSeconds"] >= 300,
            f"got {probe['failureThreshold']} * {probe['periodSeconds']}s",
        )


def shim_container() -> dict:
    return pod_spec(golden(ENGINE, "slim", "local"), "dfe-keda-shim")["containers"][0]


def mebibytes(quantity: str) -> int:
    units = {"Mi": 1, "Gi": 1024}
    return int(quantity[:-2]) * units[quantity[-2:]]


def test_the_keda_shim_gets_a_boot_budget() -> None:
    """The shim boots the full engine image, about 40s at a 100m CPU cap, and its
    liveness gave up at about 28s, so every start was killed before it listened."""
    container = shim_container()
    probe = container.get("startupProbe")
    expect("the keda-shim container has a startupProbe", probe is not None, "none rendered")
    if probe:
        expect(
            "and it targets the liveness path on the shim's own port",
            probe["httpGet"] == container["livenessProbe"]["httpGet"],
            f"got {probe['httpGet']!r}",
        )
        expect(
            "with a budget well past a throttled boot",
            probe["failureThreshold"] * probe["periodSeconds"] >= 150,
            f"got {probe['failureThreshold']} * {probe['periodSeconds']}s",
        )


def test_the_keda_shim_has_room_for_the_engine_image() -> None:
    """The engine image sits at about 126Mi resident once booted, so a 128Mi
    limit leaves the shim a few MiB from the OOM killer."""
    resources = shim_container()["resources"]
    limit = mebibytes(resources["limits"]["memory"])
    request = mebibytes(resources["requests"]["memory"])
    expect("the shim's memory limit is at least 256Mi", limit >= 256, f"got {limit}Mi")
    expect("and its request covers the resident size", request >= 128, f"got {request}Mi")


def test_an_external_tls_clickhouse_is_expressible() -> None:
    """A deployment whose ClickHouse speaks TLS could not be rendered at all (#277).
    A deployment that turns TLS off stays plaintext; the cascade's own TLS is
    test_clickhouse_tls.py's."""
    env = engine_env(golden(ENGINE, "slim", "local", "clickhouse-plaintext"))
    expect(
        "a deployment with TLS off is plaintext",
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
    external = {
        "clickhouse": {
            "mode": "external",
            "host": "clickhouse.example.com",
            "tls": {"enabled": True, "verify": False, "ca": {"secretName": ""}},
        }
    }
    tls = literal_env("scale", infra=external)
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
        "DFE_API_JWT_EXPIRE_MINUTES" not in engine_env(golden(ENGINE, "slim", "local")),
        "the variable was rendered with nothing behind it",
    )
    shortened = literal_env(overlay=nested({"api.jwtExpireMinutes": 15}))
    got = shortened.get("DFE_API_JWT_EXPIRE_MINUTES")
    expect("and a deployment can shorten it", got == "15", f"got {got!r}")


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


def test_an_unset_auth_dial_renders_no_variable() -> None:
    """Absent, the engine takes its own default; an empty variable is not absent."""
    for profile, cloud in CELLS:
        env = engine_env(golden(ENGINE, profile, cloud))
        for key, name in AUTH_DIALS.items():
            got = env.get(name)
            expect(f"{profile}/{cloud}: unset {key} renders no {name}", name not in env,
                   f"got {got!r}")


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
    env = engine_env(render_app(ENGINE, "slim", "local", overlay=nested(sets)))
    for key, value in sets.items():
        name = AUTH_DIALS[key]
        expect(f"{key}={value} renders {name}", env.get(name) == value, f"got {env.get(name)!r}")


def test_a_zero_key_lifetime_is_rendered_not_dropped() -> None:
    """0 is a key with no expiry, the one value a truthiness test would lose."""
    name = AUTH_DIALS["auth.apiKeyDefaultTtlDays"]
    got = literal_env(overlay=nested({"auth.apiKeyDefaultTtlDays": 0})).get(name)
    expect("apiKeyDefaultTtlDays=0 reaches the engine as 0", got == "0", f"got {got!r}")


def test_a_count_from_a_values_file_renders_whole() -> None:
    """A values-file number is a float64, which renders 1e+06 bare."""
    dials = {
        "api.maxSessionMinutes": 1000000,
        "auth.loginThrottle.maxDelaySeconds": 900,
        "auth.loginThrottle.enabled": True,
    }
    env = literal_env(overlay=nested(dials))
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
        ("api.docsEnabled", "yes"),
        ("auth.loginThrottle.enabled", "off"),
        ("auth.loginThrottle.usernameFailures", 0),
        ("auth.loginThrottle.clientFailures", 0),
        ("auth.loginThrottle.maxDelaySeconds", 0),
        ("api.maxSessionMinutes", 0),
        ("api.maxSessionMinutes", 1.5),
        ("auth.apiKeyDefaultTtlDays", -1),
        ("auth.apiKeyDefaultTtlDays", "90d"),
    )
    for key, value in cases:
        err = refusal(overlay=nested({key: value}))
        expect(f"{key}={value!r} is refused naming {key}", key in err, f"refusal={err[-300:]!r}")


FORWARDED_ENV = "DFE_API_FORWARDED_ALLOW_IPS"


def test_the_proxy_hops_default_to_the_deployments_pod_range() -> None:
    """Behind the gateway every request arrives from a pod. Unlisted, the engine
    builds http OIDC callbacks and audits the gateway's address for everyone."""
    common = yaml.safe_load((VALUES / "common.yaml").read_text(encoding="utf-8"))
    pod_cidr = common["networkModel"]["podCIDR"]
    expect("common.yaml declares a pod range", bool(pod_cidr), f"got {pod_cidr!r}")
    for profile, cloud in CELLS:
        got = engine_env(golden(ENGINE, profile, cloud)).get(FORWARDED_ENV)
        expect(f"{profile}/{cloud}: the deployment trusts that range", got == pod_cidr,
               f"got {got!r}")
    moved = literal_env(infra={"networkModel": {"podCIDR": "10.42.0.0/16"}})
    expect("and follows it when a deployment moves it",
           moved.get(FORWARDED_ENV) == "10.42.0.0/16", f"got {moved.get(FORWARDED_ENV)!r}")


def test_a_named_hop_list_beats_the_pod_range() -> None:
    hops = "10.42.7.0/24,10.42.8.0/24"
    got = literal_env(overlay={"api": {"forwardedAllowIps": hops}}).get(FORWARDED_ENV)
    expect("api.forwardedAllowIps wins", got == hops, f"got {got!r}")


def test_a_render_with_no_pod_range_keeps_the_engines_default() -> None:
    """A deployment that declares no pod range trusts no hop, and the engine's
    loopback default fails closed there."""
    got = literal_env(infra={"networkModel": {"podCIDR": ""}}).get(FORWARDED_ENV)
    expect("no pod range renders no hop list", got is None, f"got {got!r}")


TOPIC_SIZE_ENV = "DFE_KAFKA_TOPIC_MAX_MESSAGE_BYTES"
TOPIC_SIZE_ANNOTATION = "dfe.hyperi.io/kafka_message_max_bytes"


def test_the_topic_size_reaches_the_engine_only_when_set() -> None:
    """A managed broker refuses a record above its provider default, so the
    engine's own topics take the size the landing topics were created at."""
    for profile in BUS_PROFILES:
        expect(
            f"{profile}: unset leaves the engine's own default",
            TOPIC_SIZE_ENV not in engine_env(golden(ENGINE, profile, "local")),
            "the variable was rendered with nothing behind it",
        )
    # layer2-apps.yaml hands the annotation over as a quoted string.
    managed = {**MANAGED_BROKER, TOPIC_SIZE_ANNOTATION: "8388608"}
    got = literal_env("single", annotations=managed).get(TOPIC_SIZE_ENV)
    expect("the cluster secret's size reaches the container", got == "8388608", f"got {got!r}")
    got = literal_env("single", annotations={TOPIC_SIZE_ANNOTATION: "8388608"}).get(TOPIC_SIZE_ENV)
    expect("and does with no kafka_mode beside it", got == "8388608", f"got {got!r}")
    # A values file reads a number as a float64, which renders as 1.6777216e+07 bare.
    got = literal_env("single", overlay={"kafka": {"messageMaxBytes": 16777216}}).get(
        TOPIC_SIZE_ENV
    )
    expect("a number from a values file renders as whole bytes", got == "16777216", f"got {got!r}")


def test_a_topic_size_that_is_not_bytes_fails_the_render() -> None:
    """A unit suffix would reach the engine as a setting it cannot parse."""
    err = refusal("single", annotations={**MANAGED_BROKER, TOPIC_SIZE_ANNOTATION: "16MiB"})
    expect(
        "the render refuses it by name", "kafka.messageMaxBytes" in err, f"refusal={err[-300:]!r}"
    )


ENSURE_TOPICS_ENV = "DFE_KAFKA_ENSURE_TOPICS"


def test_the_boot_topic_pass_is_turned_on_only_on_the_bus() -> None:
    """The engine's boot pass runs only when the dial is explicitly true, so a bus
    deployment left to the derived default loses every source's topics on a broker
    rebuild, and a brokerless one has no broker to ask."""
    for mode in ("single", "cluster", "external"):
        got = literal_env("single", infra={"kafka": {"mode": mode}}).get(ENSURE_TOPICS_ENV)
        expect(f"kafka.mode={mode} turns the boot pass on", got == "true", f"got {got!r}")
    got = engine_env(render_app(ENGINE, "", "local")).get(ENSURE_TOPICS_ENV)
    expect("an unset kafka.mode is the bus and turns it on", got == "true", f"got {got!r}")
    got = literal_env("slim", infra={"kafka": {"mode": "disabled"}}).get(ENSURE_TOPICS_ENV)
    expect("a brokerless deployment renders no dial", got is None, f"got {got!r}")


def test_each_profile_sets_the_boot_topic_pass_by_its_transport() -> None:
    transports = set()
    for profile, cloud in CELLS:
        env = engine_env(golden(ENGINE, profile, cloud))
        transports.add(env.get("DFE_TRANSPORT_DEFAULT"))
        expected = "true" if env.get("DFE_TRANSPORT_DEFAULT") == "bus" else None
        expect(
            f"{profile}/{cloud} sets the boot topic pass only when it carries a bus",
            env.get(ENSURE_TOPICS_ENV) == expected,
            f"transport={env.get('DFE_TRANSPORT_DEFAULT')!r} dial={env.get(ENSURE_TOPICS_ENV)!r}",
        )
    expect(
        "the profiles cover both a bus and a brokerless transport",
        transports == {"bus", "direct"},
        f"got {sorted(transports, key=str)!r}",
    )


def test_every_profile_still_renders_with_the_real_overlays() -> None:
    for profile, cloud in CELLS:
        env = engine_env(golden(ENGINE, profile, cloud))
        expect(
            f"{profile}/{cloud} renders the engine with its tier",
            env.get("DFE_PROFILE") == profile,
            f"got {env.get('DFE_PROFILE')!r}",
        )
        expect(
            f"{profile}/{cloud} still carries the transport facts",
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
        test_a_manifest_edit_rolls_the_engine()
        test_an_auth_configmap_edit_can_reach_the_engine()
        test_an_unset_auth_configmap_is_not_mounted()
        test_the_engine_gets_a_boot_budget()
        test_the_keda_shim_gets_a_boot_budget()
        test_the_keda_shim_has_room_for_the_engine_image()
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
        test_the_boot_topic_pass_is_turned_on_only_on_the_bus()
        test_each_profile_sets_the_boot_topic_pass_by_its_transport()
        test_every_profile_still_renders_with_the_real_overlays()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
