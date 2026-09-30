#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_clickhouse_tls.py
#  Purpose:      Prove the Kubernetes cascade serves ClickHouse over TLS and that
#                every in-cluster client dials it verified, from one values block.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse over TLS on every deployment that carries a CA.

argocd/values/common.yaml's `clickhouse.tls` is read by the server chart and by
every client chart, so one block decides the whole hop. These checks tie the
halves together: the certificate names the host the clients dial, the CA Secret
the server chart fills is the one the clients mount, and each client verifies
with it rather than trusting on first use. A render with no CA (every chart's
own defaults, which the dfe-stack trial uses) stays on plaintext.

    python3 scripts/tests/test_clickhouse_tls.py

Needs `helm` on PATH. Runs under pytest too, which is how CI reaches it.
"""

from __future__ import annotations

import functools
import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
COMMON = yaml.safe_load((VALUES / "common.yaml").read_text(encoding="utf-8"))
TLS = COMMON["clickhouse"]["tls"]

# Each tier's server mode and the host its clients dial.
TIERS = ("slim", "single", "scale", "mesh")

APP_NS = "dfe"
DATA_NS = "clickhouse"


@functools.cache
def render(chart: str, *args: str, namespace: str = APP_NS) -> tuple[dict, ...]:
    cmd = ["helm", "template", chart, str(chart_dir(chart)), "--namespace", namespace, *args]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart} {args}:\n{out.stderr}")
    return tuple(d for d in yaml.safe_load_all(out.stdout) if d)


def refusal(chart: str, *args: str) -> str:
    """The render's stderr, or empty when it rendered."""
    cmd = ["helm", "template", chart, str(chart_dir(chart)), *args]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    return out.stderr if out.returncode != 0 else ""


def cascade(tier: str) -> tuple[str, ...]:
    return ("-f", str(VALUES / "common.yaml"), "-f", str(VALUES / f"profile-{tier}.yaml"))


def dialled_host(tier: str) -> str:
    profile = yaml.safe_load((VALUES / f"profile-{tier}.yaml").read_text(encoding="utf-8"))
    return (profile.get("clickhouse") or {}).get("host") or COMMON["clickhouse"]["host"]


def server(tier: str) -> tuple[dict, ...]:
    return render("clickhouse-cluster", *cascade(tier), "--set", f"appNamespace={APP_NS}",
                  namespace=DATA_NS)


def one(docs: tuple[dict, ...], kind: str, name: str | None = None) -> dict | None:
    return next(
        (d for d in docs if d.get("kind") == kind and (name is None or d["metadata"]["name"] == name)),
        None,
    )


def pods(docs: tuple[dict, ...]) -> dict[str, dict]:
    return {
        d["metadata"]["name"]: d["spec"]["template"]["spec"]
        for d in docs
        if d.get("kind") in ("Deployment", "StatefulSet")
    }


def env_of(container: dict) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for e in container.get("env") or []}


def mounted_file(pod: dict, container: dict, path: str) -> dict | None:
    """The volume that supplies `path` to the container, or None."""
    for mount in container.get("volumeMounts") or []:
        root = mount["mountPath"].rstrip("/") + "/"
        if path.startswith(root) and "subPath" not in mount:
            return next((v for v in pod.get("volumes") or [] if v["name"] == mount["name"]), None)
    return None


def test_the_cascade_turns_tls_on_with_an_issuer() -> None:
    expect("common.yaml turns ClickHouse TLS on", TLS["enabled"] is True, f"got {TLS['enabled']!r}")
    expect("and names the issuer that signs it", bool(TLS["issuerRef"]["name"]), f"got {TLS!r}")
    expect("with the plaintext ports still open", TLS["required"] is False, f"got {TLS['required']!r}")


def test_the_certificate_names_the_host_each_tier_dials() -> None:
    for tier in TIERS:
        cert = one(server(tier), "Certificate")
        expect(f"{tier} renders the server certificate", cert is not None, "none rendered")
        if cert is None:
            continue
        host = dialled_host(tier)
        expect(f"{tier} certificate covers {host}", host in cert["spec"]["dnsNames"],
               f"got {cert['spec']['dnsNames']}")
        expect(f"{tier} certificate comes from the configured issuer",
               cert["spec"]["issuerRef"]["name"] == TLS["issuerRef"]["name"],
               f"got {cert['spec']['issuerRef']!r}")


def test_cluster_mode_hands_the_certificate_to_the_operator() -> None:
    for tier in ("scale", "mesh"):
        docs = server(tier)
        cr = one(docs, "ClickHouseCluster")
        cert = one(docs, "Certificate")
        tls = (cr or {}).get("spec", {}).get("settings", {}).get("tls")
        expect(f"{tier} operator TLS is on and optional",
               tls is not None and tls["enabled"] is True and tls["required"] is False, f"got {tls!r}")
        if tls and cert:
            expect(f"{tier} operator reads the Secret the certificate writes",
                   tls["serverCertSecret"]["name"] == cert["spec"]["secretName"], f"got {tls!r}")


def test_single_mode_serves_https_from_the_certificate() -> None:
    for tier in ("slim", "single"):
        docs = server(tier)
        sts = pods(docs).get("dfe-clickhouse")
        cert = one(docs, "Certificate")
        service = one(docs, "Service", "dfe-clickhouse")
        config = one(docs, "ConfigMap", "dfe-clickhouse-custom-config")
        expect(f"{tier} renders the single-node server", sts is not None and cert is not None, "")
        if not (sts and cert and service and config):
            continue
        container = sts["containers"][0]
        expect(f"{tier} container exposes 8443",
               any(p["containerPort"] == 8443 for p in container["ports"]), f"{container['ports']}")
        expect(f"{tier} Service exposes 8443",
               any(p["port"] == 8443 for p in service["spec"]["ports"]), f"{service['spec']['ports']}")
        tls_xml = config["data"].get("dfe-tls.xml", "")
        expect(f"{tier} config.d opens https_port 8443", "<https_port>8443</https_port>" in tls_xml,
               tls_xml)
        for leaf in ("tls.crt", "tls.key"):
            path = f"/etc/clickhouse-server/tls/{leaf}"
            volume = mounted_file(sts, container, path)
            expect(f"{tier} {leaf} comes from the certificate's Secret, mounted as a directory",
                   volume is not None and volume.get("secret", {}).get("secretName")
                   == cert["spec"]["secretName"] and path in tls_xml, f"got {volume!r}")


def test_the_ca_copy_lands_where_the_clients_read_it() -> None:
    docs = server("scale")
    copy = next((d for d in docs if d.get("kind") == "ClusterExternalSecret"
                 and d["spec"]["externalSecretName"] == TLS["ca"]["secretName"]), None)
    role = one(docs, "Role")
    cert = one(docs, "Certificate")
    expect("the server chart copies the CA into the app namespace", copy is not None, "no copy")
    if copy and cert:
        spec = copy["spec"]["externalSecretSpec"]
        data = spec["data"]
        expect("only ca.crt crosses",
               data == [{"secretKey": TLS["ca"]["dataKey"],
                         "remoteRef": {"key": cert["spec"]["secretName"], "property": "ca.crt"}}],
               f"got {data!r}")
        expect("into the Secret common.yaml points every client at",
               spec["target"]["name"] == TLS["ca"]["secretName"], f"got {spec['target']!r}")
        expect("and the reader may get the certificate's Secret",
               cert["spec"]["secretName"] in role["rules"][0]["resourceNames"], f"got {role['rules']}")


def ca_volume_holds(volume: dict | None) -> bool:
    return bool(volume) and volume.get("secret", {}).get("secretName") == TLS["ca"]["secretName"]


def test_every_engine_pod_dials_tls_and_verifies_with_the_ca() -> None:
    for tier in TIERS:
        workloads = pods(render("dfe-engine", *cascade(tier)))
        for name in ("dfe-engine", "dfe-hunt-runner", "dfe-keda-shim"):
            pod = workloads.get(name)
            expect(f"{tier} renders {name}", pod is not None, "")
            if pod is None:
                continue
            container = pod["containers"][0]
            env = env_of(container)
            expect(f"{tier} {name} dials HTTPS on the TLS port",
                   env.get("DFE_CLICKHOUSE_SECURE") == "true"
                   and env.get("DFE_CLICKHOUSE_PORT") == str(TLS["port"]),
                   f"got {env.get('DFE_CLICKHOUSE_SECURE')!r} {env.get('DFE_CLICKHOUSE_PORT')!r}")
            expect(f"{tier} {name} verifies", env.get("DFE_CLICKHOUSE_VERIFY") == "true",
                   f"got {env.get('DFE_CLICKHOUSE_VERIFY')!r}")
            ca = env.get("DFE_CLICKHOUSE_CA_CERT", "")
            expect(f"{tier} {name} names a CA file the ClickHouse CA Secret supplies",
                   ca.endswith("/" + TLS["ca"]["dataKey"]) and ca_volume_holds(
                       mounted_file(pod, container, ca)), f"got {ca!r}")


def test_the_loader_dials_8443_through_a_merged_trust_store() -> None:
    for tier in TIERS:
        pod = pods(render("dfe-loader", *cascade(tier))).get("dfe-loader")
        if pod is None:
            expect(f"{tier} renders dfe-loader", False, "")
            continue
        container = pod["containers"][0]
        env = env_of(container)
        expect(f"{tier} loader dials the TLS port",
               env.get("DFE_LOADER_CLICKHOUSE_HOSTS") == f"{dialled_host(tier)}:{TLS['port']}",
               f"got {env.get('DFE_LOADER_CLICKHOUSE_HOSTS')!r}")
        bundle = env.get("SSL_CERT_FILE", "")
        init = next((c for c in pod.get("initContainers") or []
                     if c["name"] == "clickhouse-ca-bundle"), None)
        script = " ".join((init or {}).get("command") or [])
        expect(f"{tier} the init container writes the file SSL_CERT_FILE names",
               bool(bundle) and f"> {bundle}" in script, f"SSL_CERT_FILE={bundle!r}")
        expect(f"{tier} from the image's own roots plus the ClickHouse CA",
               "/etc/ssl/certs/ca-certificates.crt" in script
               and any(ca_volume_holds(mounted_file(pod, init, part))
                       for part in script.split() if part.startswith("/etc/dfe-clickhouse-ca/")),
               script)
        expect(f"{tier} and the loader mounts that file",
               mounted_file(pod, container, bundle) is not None, f"no mount for {bundle!r}")


def test_hyperdx_adds_the_ca_to_node() -> None:
    for tier in TIERS:
        pod = pods(render("hyperdx", *cascade(tier))).get("dfe-hyperdx")
        if pod is None:
            expect(f"{tier} renders hyperdx", False, "")
            continue
        container = pod["containers"][0]
        path = env_of(container).get("NODE_EXTRA_CA_CERTS", "")
        expect(f"{tier} hyperdx trusts the ClickHouse CA",
               ca_volume_holds(mounted_file(pod, container, path)), f"got {path!r}")


def test_app_egress_reaches_the_tls_ports() -> None:
    policy = one(render("network-policies", "-f", str(VALUES / "common.yaml"),
                        "--set", "dfeNamespaces={dfe}"), "NetworkPolicy", "allow-dfe-backing-egress")
    rule = next((r for r in (policy or {}).get("spec", {}).get("egress", [])
                 if r["to"][0]["namespaceSelector"]["matchLabels"].get(
                     "kubernetes.io/metadata.name") == DATA_NS), None)
    ports = {p["port"] for p in (rule or {}).get("ports", [])}
    expect("apps may reach ClickHouse on 8443 and 9440", {8443, 9440} <= ports, f"got {ports}")


def test_a_render_with_no_ca_stays_plaintext() -> None:
    """Every chart's own default, which is what the dfe-stack trial renders."""
    docs = render("clickhouse-cluster", "--set", f"appNamespace={APP_NS}", namespace=DATA_NS)
    expect("no certificate", one(docs, "Certificate") is None, "")
    expect("no operator TLS", "tls" not in one(docs, "ClickHouseCluster")["spec"]["settings"], "")
    engine = env_of(pods(render("dfe-engine"))["dfe-engine"]["containers"][0])
    expect("the engine dials plaintext 8123",
           engine.get("DFE_CLICKHOUSE_SECURE") == "false" and engine.get("DFE_CLICKHOUSE_PORT") == "8123",
           f"got {engine}")
    loader_pod = pods(render("dfe-loader"))["dfe-loader"]
    loader = env_of(loader_pod["containers"][0])
    expect("the loader dials 8123 with no trust store of its own",
           loader.get("DFE_LOADER_CLICKHOUSE_HOSTS", "").endswith(":8123") and "SSL_CERT_FILE" not in loader,
           f"got {loader.get('DFE_LOADER_CLICKHOUSE_HOSTS')!r}")
    hyperdx = env_of(pods(render("hyperdx"))["dfe-hyperdx"]["containers"][0])
    expect("hyperdx adds no CA", "NODE_EXTRA_CA_CERTS" not in hyperdx, "")


def test_an_external_server_renders_no_certificate() -> None:
    docs = render("clickhouse-cluster", *cascade("scale"), "--set", "clickhouse.mode=external",
                  "--set", f"appNamespace={APP_NS}", namespace=DATA_NS)
    expect("external mode issues nothing", one(docs, "Certificate") is None, "")
    expect("and copies no CA", not any(d.get("kind") == "ClusterExternalSecret" for d in docs), "")


def test_a_public_ca_needs_no_mount() -> None:
    """An external server with a public certificate verifies against system roots."""
    args = (*cascade("scale"), "--set", "clickhouse.host=abc.clickhouse.cloud",
            "--set", "clickhouse.tls.ca.secretName=")
    engine_pod = pods(render("dfe-engine", *args))["dfe-engine"]
    env = env_of(engine_pod["containers"][0])
    expect("the engine still dials TLS", env.get("DFE_CLICKHOUSE_SECURE") == "true", f"{env}")
    expect("with no CA file", "DFE_CLICKHOUSE_CA_CERT" not in env, f"{env}")
    loader_pod = pods(render("dfe-loader", *args))["dfe-loader"]
    expect("and the loader keeps its system store",
           "SSL_CERT_FILE" not in env_of(loader_pod["containers"][0])
           and not any(c["name"] == "clickhouse-ca-bundle" for c in loader_pod.get("initContainers") or []),
           "")


def test_a_setting_a_client_cannot_honour_fails_the_render() -> None:
    cases = (
        ("clickhouse-cluster", ("--set", "clickhouse.tls.enabled=true"), "clickhouse.tls.issuerRef.name"),
        ("clickhouse-cluster", (*cascade("slim"), "--set", "clickhouse.tls.required=true"),
         "clickhouse.tls.required"),
        ("dfe-loader", (*cascade("scale"), "--set", "clickhouse.tls.verify=false"),
         "clickhouse.tls.verify"),
        ("dfe-loader", (*cascade("scale"), "--set", "clickhouse.tls.port=443"), "clickhouse.tls.port"),
        ("dfe-engine", ("--set", "clickhouse.secure=true"), "clickhouse.tls.enabled"),
        ("dfe-engine", (*cascade("scale"), "--set", "clickhouse.tls.ca.configMapName=corp-ca"),
         "clickhouse.tls.ca"),
    )
    for chart, args, key in cases:
        err = refusal(chart, *args)
        expect(f"{chart} {args[-1]} is refused naming {key}", key in err, f"stderr={err[-300:]!r}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
