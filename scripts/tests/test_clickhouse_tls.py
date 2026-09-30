#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_clickhouse_tls.py
#  Purpose:      Prove the Kubernetes cascade serves ClickHouse over TLS wherever
#                it carries a CA, that every in-cluster client dials it verified,
#                and that the server keypair never leaves its namespace.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse over TLS on every deployment that carries a CA, and HTTP where none.

argocd/values/common.yaml's `clickhouse.tls` is read by the server chart and by
every client chart, so one block decides the whole hop. These checks tie the
halves together: the certificate names the host the clients dial, the CA Secret
the server chart fills is the one the clients mount, each client verifies with
it, and the server and clients agree on the scheme whether or not the edge
module (and so the internal CA) is deployed.

The keypair Secret carries tls.key, so the only reader allowed near it is a
namespaced SecretStore in the ClickHouse namespace; what crosses to the apps is
a CA-only copy, through a ClusterSecretStore scoped to the namespaces it serves.

    python3 scripts/tests/test_clickhouse_tls.py

Needs `helm` on PATH. Runs under pytest too, which is how CI reaches it.
"""

from __future__ import annotations

import functools
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
APPSETS = REPO_ROOT / "argocd" / "appsets"
COMMON = yaml.safe_load((VALUES / "common.yaml").read_text(encoding="utf-8"))
TLS = COMMON["clickhouse"]["tls"]
KEYPAIR = "dfe-clickhouse-tls"
CA_ONLY = f"{KEYPAIR}-ca"

# Each tier's server mode and the host its clients dial.
TIERS = ("slim", "single", "scale", "mesh")

APP_NS = "dfe"
DATA_NS = "clickhouse"

# The parameter both appsets set from the edge label, and what that label says.
PRESENT = "clickhouse.tls.internalCA.present"
EDGE_OFF = ("--set", f"{PRESENT}=false")
BYO_ISSUER = ("--set", "clickhouse.tls.issuerRef.name=estate-pki")

CLIENTS = ("dfe-engine", "dfe-loader", "hyperdx")


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


def server(tier: str, *extra: str) -> tuple[dict, ...]:
    return render("clickhouse-cluster", *cascade(tier), "--set", f"appNamespace={APP_NS}", *extra,
                  namespace=DATA_NS)


def one(docs: tuple[dict, ...], kind: str, name: str | None = None) -> dict | None:
    return next(
        (d for d in docs if d.get("kind") == kind and (name is None or d["metadata"]["name"] == name)),
        None,
    )


def every(docs: tuple[dict, ...], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


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


def ca_volume_holds(volume: dict | None) -> bool:
    return bool(volume) and volume.get("secret", {}).get("secretName") == TLS["ca"]["secretName"]


def client_scheme(chart: str, *args: str) -> str:
    """"https" or "http", as the client chart renders its ClickHouse connection."""
    workloads = pods(render(chart, *args))
    if chart == "dfe-engine":
        env = env_of(workloads["dfe-engine"]["containers"][0])
        return "https" if env.get("DFE_CLICKHOUSE_SECURE") == "true" else "http"
    if chart == "dfe-loader":
        env = env_of(workloads["dfe-loader"]["containers"][0])
        return "https" if env.get("DFE_LOADER_CLICKHOUSE_HOSTS", "").endswith(":8443") else "http"
    env = env_of(workloads["dfe-hyperdx"]["containers"][0])
    return "https" if "NODE_EXTRA_CA_CERTS" in env else "http"


# --- the cascade's own values -------------------------------------------------


def test_the_cascade_turns_tls_on_where_the_internal_ca_is() -> None:
    expect("common.yaml turns ClickHouse TLS on", TLS["enabled"] is True, f"got {TLS['enabled']!r}")
    expect("signing with the internal CA unless an issuer is named",
           TLS["issuerRef"]["name"] == "" and TLS["internalCA"]["issuerName"] == "dfe-internal-ca",
           f"got {TLS!r}")
    expect("which bootstrap's default edge-on deploys", TLS["internalCA"]["present"] is True,
           f"got {TLS['internalCA']!r}")
    expect("with the plaintext ports still open", TLS["required"] is False, f"got {TLS['required']!r}")


# --- the server --------------------------------------------------------------


def test_the_certificate_names_the_host_each_tier_dials() -> None:
    for tier in TIERS:
        cert = one(server(tier), "Certificate")
        expect(f"{tier} renders the server certificate", cert is not None, "none rendered")
        if cert is None:
            continue
        host = dialled_host(tier)
        expect(f"{tier} certificate covers {host}", host in cert["spec"]["dnsNames"],
               f"got {cert['spec']['dnsNames']}")
        expect(f"{tier} certificate comes from the internal CA",
               cert["spec"]["issuerRef"]["name"] == TLS["internalCA"]["issuerName"],
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


# --- the keypair stays in its namespace ---------------------------------------


def role_secrets(docs: tuple[dict, ...], role: str) -> set[str]:
    found = one(docs, "Role", role)
    return {n for rule in (found or {}).get("rules", []) for n in rule.get("resourceNames", [])}


def store_role(docs: tuple[dict, ...], store: dict) -> str | None:
    """The Role bound to the ServiceAccount a store authenticates as."""
    account = store["spec"]["provider"]["kubernetes"]["auth"]["serviceAccount"]["name"]
    for binding in every(docs, "RoleBinding"):
        if any(s["kind"] == "ServiceAccount" and s["name"] == account for s in binding["subjects"]):
            return binding["roleRef"]["name"]
    return None


def test_no_cluster_scoped_store_can_read_the_keypair() -> None:
    """A ClusterSecretStore serves ExternalSecrets in other namespaces, so the
    Secret holding tls.key must be outside what any of them can read."""
    for tier in TIERS:
        docs = server(tier)
        cluster_stores = every(docs, "ClusterSecretStore")
        expect(f"{tier} renders the cross-namespace store", bool(cluster_stores), "none rendered")
        for store in cluster_stores:
            names = role_secrets(docs, store_role(docs, store) or "")
            expect(f"{tier} {store['metadata']['name']} cannot read {KEYPAIR}",
                   KEYPAIR not in names and CA_ONLY in names, f"its Role names {sorted(names)}")


def test_the_keypair_has_one_reader_and_it_is_namespaced() -> None:
    for tier in TIERS:
        docs = server(tier)
        readers = [r["metadata"]["name"] for r in every(docs, "Role")
                   if any(KEYPAIR in rule.get("resourceNames", []) for rule in r["rules"])]
        expect(f"{tier} exactly one Role names {KEYPAIR}", len(readers) == 1, f"got {readers}")
        stores = [s for s in every(docs, "SecretStore") + every(docs, "ClusterSecretStore")
                  if store_role(docs, s) in readers]
        expect(f"{tier} and only a namespaced SecretStore uses it",
               [s["kind"] for s in stores] == ["SecretStore"], f"got {[s['kind'] for s in stores]}")
        copy = one(docs, "ExternalSecret", CA_ONLY)
        expect(f"{tier} which copies ca.crt alone into {CA_ONLY}",
               copy is not None and copy["spec"]["target"]["name"] == CA_ONLY
               and copy["spec"]["data"] == [{"secretKey": "ca.crt",
                                             "remoteRef": {"key": KEYPAIR, "property": "ca.crt"}}],
               f"got {copy!r}")


def test_the_ca_copy_lands_where_the_clients_read_it() -> None:
    docs = server("scale")
    copy = next((d for d in every(docs, "ClusterExternalSecret")
                 if d["spec"]["externalSecretName"] == TLS["ca"]["secretName"]), None)
    expect("the server chart copies the CA into the app namespace", copy is not None, "no copy")
    if copy:
        spec = copy["spec"]["externalSecretSpec"]
        expect("from the CA-only Secret, never the keypair",
               spec["data"] == [{"secretKey": TLS["ca"]["dataKey"],
                                 "remoteRef": {"key": CA_ONLY, "property": "ca.crt"}}],
               f"got {spec['data']!r}")
        expect("into the Secret common.yaml points every client at",
               spec["target"]["name"] == TLS["ca"]["secretName"], f"got {spec['target']!r}")


def selected(copy: dict) -> set[str]:
    return {s["matchLabels"]["kubernetes.io/metadata.name"] for s in copy["spec"]["namespaceSelectors"]}


def test_the_store_serves_only_the_namespaces_it_copies_into() -> None:
    for extra in ((), ("--set", "extraCredentialNamespaces={otel,audit}")):
        docs = server("scale", *extra)
        for store in every(docs, "ClusterSecretStore"):
            name = store["metadata"]["name"]
            conditions = store["spec"].get("conditions") or []
            allowed = {n for c in conditions for n in c.get("namespaces", [])}
            copies = [c for c in every(docs, "ClusterExternalSecret")
                      if c["spec"]["externalSecretSpec"]["secretStoreRef"]["name"] == name]
            targets = set().union(*(selected(c) for c in copies)) if copies else set()
            expect(f"{name} carries namespace conditions {extra}", bool(conditions), "none")
            expect(f"{name} admits exactly the namespaces its copies land in {extra}",
                   allowed == targets and bool(targets), f"conditions {allowed}, copies {targets}")


# --- the clients -------------------------------------------------------------


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


def test_hyperdx_adds_the_ca_to_node_and_rolls_on_a_new_one() -> None:
    for tier in TIERS:
        docs = render("hyperdx", *cascade(tier))
        pod = pods(docs).get("dfe-hyperdx")
        if pod is None:
            expect(f"{tier} renders hyperdx", False, "")
            continue
        container = pod["containers"][0]
        path = env_of(container).get("NODE_EXTRA_CA_CERTS", "")
        expect(f"{tier} hyperdx trusts the ClickHouse CA",
               ca_volume_holds(mounted_file(pod, container, path)), f"got {path!r}")
        annotations = one(docs, "Deployment", "dfe-hyperdx")["metadata"].get("annotations") or {}
        expect(f"{tier} and Reloader restarts it when that Secret changes",
               annotations.get("reloader.stakater.com/auto") == "true", f"got {annotations}")


def test_app_egress_reaches_the_tls_ports() -> None:
    policy = one(render("network-policies", "-f", str(VALUES / "common.yaml"),
                        "--set", "dfeNamespaces={dfe}"), "NetworkPolicy", "allow-dfe-backing-egress")
    rule = next((r for r in (policy or {}).get("spec", {}).get("egress", [])
                 if r["to"][0]["namespaceSelector"]["matchLabels"].get(
                     "kubernetes.io/metadata.name") == DATA_NS), None)
    ports = {p["port"] for p in (rule or {}).get("ports", [])}
    expect("apps may reach ClickHouse on 8443 and 9440", {8443, 9440} <= ports, f"got {ports}")


# --- edge on, edge off, and a named issuer ------------------------------------


def appset_parameter(appset: str, name: str) -> str:
    doc = yaml.safe_load((APPSETS / appset).read_text(encoding="utf-8"))
    params = doc["spec"]["template"]["spec"]["sources"][0]["helm"]["parameters"]
    return next((p["value"] for p in params if p["name"] == name), "")


def execute(template: str, labels: dict[str, str]) -> str:
    """The appset expression, run by Helm's text/template and sprig -- the
    library the ApplicationSet controller renders goTemplate with."""
    with tempfile.TemporaryDirectory() as tmp:
        chart = Path(tmp) / "appset"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: appset\nversion: 0.0.0\n",
                                          encoding="utf-8", newline="\n")
        # Wrapped in a ConfigMap, because Helm parses whatever it renders as a manifest.
        (chart / "templates" / "out.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: appset\ndata:\n"
            '  value: "{{- with .Values }}' + template + '{{- end }}"\n',
            encoding="utf-8", newline="\n")
        values = Path(tmp) / "labels.yaml"
        values.write_text(yaml.safe_dump({"metadata": {"labels": labels}}), encoding="utf-8",
                          newline="\n")
        out = subprocess.run(["helm", "template", "appset", str(chart), "-f", str(values)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             check=False)
    if out.returncode != 0:
        raise SystemExit(f"appset expression failed: {out.stderr}")
    return next(d for d in yaml.safe_load_all(out.stdout) if d)["data"]["value"]


def test_both_appsets_derive_the_internal_ca_from_the_edge_label() -> None:
    """The label that decides whether the gateway -- and so dfe-internal-ca -- is
    deployed: layer2-edge.yaml on "true", layer2-platform.yaml on no label."""
    data = appset_parameter("layer2-data.yaml", PRESENT)
    apps = appset_parameter("layer2-apps.yaml", PRESENT)
    expect("layer2-data hands clickhouse-cluster the fact", bool(data), "no parameter")
    expect("layer2-apps hands the clients the same expression", data == apps and bool(apps),
           f"data={data!r} apps={apps!r}")
    for labels, want in (({"dfe.hyperi.io/edge": "true"}, "true"),
                         ({"dfe.hyperi.io/edge": "false"}, "false"),
                         ({}, "true")):
        got = execute(data, labels)
        expect(f"edge label {labels or 'absent'} means the internal CA present={want}",
               got == want, f"got {got!r}")


def test_edge_on_serves_and_dials_tls() -> None:
    for tier in ("slim", "scale"):
        expect(f"{tier} edge on issues the certificate", one(server(tier), "Certificate") is not None, "")
        for chart in CLIENTS:
            expect(f"{tier} edge on: {chart} dials https",
                   client_scheme(chart, *cascade(tier)) == "https", "")


def test_edge_off_with_nothing_named_stays_on_http() -> None:
    """No CA means HTTP: no Certificate to wait on, and every client on 8123."""
    for tier in ("slim", "scale"):
        docs = server(tier, *EDGE_OFF)
        expect(f"{tier} edge off issues no certificate", one(docs, "Certificate") is None, "")
        expect(f"{tier} edge off renders no keypair reader or CA copy",
               one(docs, "SecretStore") is None and one(docs, "ExternalSecret", CA_ONLY) is None
               and not any(c["spec"]["externalSecretName"] == TLS["ca"]["secretName"]
                           for c in every(docs, "ClusterExternalSecret")), "")
        cr = one(docs, "ClickHouseCluster")
        expect(f"{tier} edge off gives the operator no TLS",
               cr is None or "tls" not in cr["spec"]["settings"], "")
        sts = pods(docs).get("dfe-clickhouse")
        expect(f"{tier} edge off opens no 8443",
               sts is None or not any(p["containerPort"] == 8443 for p in sts["containers"][0]["ports"]),
               "")
        for chart in CLIENTS:
            expect(f"{tier} edge off: {chart} dials http",
                   client_scheme(chart, *cascade(tier), *EDGE_OFF) == "http", "")


def test_edge_off_with_a_named_issuer_serves_tls() -> None:
    for tier in ("slim", "scale"):
        cert = one(server(tier, *EDGE_OFF, *BYO_ISSUER), "Certificate")
        expect(f"{tier} edge off with an issuer requests from that issuer",
               cert is not None and cert["spec"]["issuerRef"]["name"] == "estate-pki",
               f"got {cert and cert['spec']['issuerRef']}")
        for chart in CLIENTS:
            expect(f"{tier} edge off with an issuer: {chart} dials https",
                   client_scheme(chart, *cascade(tier), *EDGE_OFF, *BYO_ISSUER) == "https", "")


def test_an_external_server_brings_its_own_certificate() -> None:
    external = ("--set", "clickhouse.mode=external", "--set", "clickhouse.host=abc.clickhouse.cloud",
                "--set", "clickhouse.tls.ca.secretName=")
    docs = server("scale", *external)
    expect("external mode issues nothing", one(docs, "Certificate") is None, "")
    expect("and copies no CA", not every(docs, "ClusterExternalSecret"), "")
    for edge in ((), EDGE_OFF):
        engine = env_of(pods(render("dfe-engine", *cascade("scale"), *external, *edge))
                        ["dfe-engine"]["containers"][0])
        expect(f"the engine dials it over TLS {edge}", engine.get("DFE_CLICKHOUSE_SECURE") == "true",
               f"{engine}")
        expect(f"against system roots {edge}", "DFE_CLICKHOUSE_CA_CERT" not in engine, f"{engine}")
        loader = pods(render("dfe-loader", *cascade("scale"), *external, *edge))["dfe-loader"]
        expect(f"and the loader keeps its system store {edge}",
               "SSL_CERT_FILE" not in env_of(loader["containers"][0])
               and not any(c["name"] == "clickhouse-ca-bundle"
                           for c in loader.get("initContainers") or []), "")


def test_a_render_with_no_ca_stays_plaintext() -> None:
    """Every chart's own default, which is what the dfe-stack trial renders."""
    docs = render("clickhouse-cluster", "--set", f"appNamespace={APP_NS}", namespace=DATA_NS)
    expect("no certificate", one(docs, "Certificate") is None, "")
    expect("no operator TLS", "tls" not in one(docs, "ClickHouseCluster")["spec"]["settings"], "")
    for chart in CLIENTS:
        expect(f"{chart} dials http", client_scheme(chart) == "http", "")
    enabled_alone = ("--set", "clickhouse.tls.enabled=true")
    expect("enabled with no CA issues nothing",
           one(render("clickhouse-cluster", *enabled_alone, namespace=DATA_NS), "Certificate") is None, "")
    for chart in CLIENTS:
        expect(f"enabled with no CA: {chart} dials http", client_scheme(chart, *enabled_alone) == "http", "")


def test_a_setting_a_client_cannot_honour_fails_the_render() -> None:
    cases = (
        ("clickhouse-cluster", (*cascade("scale"), *EDGE_OFF, "--set", "clickhouse.tls.required=true"),
         "clickhouse.tls.required"),
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
