#!/usr/bin/env bash
# bootstrap.sh — Idempotent DFE cluster bootstrap (Build 1: static/release-pinned).
#
# This script installs Layer 1 infrastructure once per cluster. It only changes
# on DFE version bumps or customer environment customization. After it completes,
# ArgoCD takes over for dynamic Layer 2 management (Build 2).
#
# All steps use 'helm upgrade --install' or 'kubectl apply' — safe to re-run.
#
# Requirements:
#   - kubectl configured and pointing at the target cluster
#   - helm 3.x installed
#   - terraform >=1.6 or opentofu >=1.6 -- OPTIONAL here (used earlier for the
#     apply; bootstrap.sh only consumes its outputs via env, never invokes it)
#   - envsubst (gettext package)
#
# Required environment variables (export from Terraform outputs):
#   DFE_ENV                  dev | test | staging | prod | local | customer-<id>
#   DFE_CLOUD                aws | gcp | azure | local -- also names the
#                            argocd/values/<cloud>.yaml overlay, which must exist
#   DFE_REGION               e.g. us-east-1, local
#   DFE_DOMAIN               e.g. dfe.example.com; derived as
#                            <DFE_PROFILE>.<DFE_BASE_DOMAIN> when unset
#   DFE_PROFILE              slim | single | scale | mesh (default: scale)
#   DFE_REPO_URL             Git repo URL for ArgoCD (the CHART source)
#   DFE_REPO_TOKEN           optional; HTTPS token when the chart repo is private
#   DFE_REPO_USER            optional; username for DFE_REPO_TOKEN (default: git)
#   DFE_REPO_SSH_KEY         optional; path to an SSH key when the chart repo is
#                            private and DFE_REPO_URL is an SSH URL
#   DFE_TARGET_REVISION      Git branch/tag (e.g. main)
#   DFE_STORAGE_CLASS        e.g. local-path, gp3, standard
#   DFE_NAMESPACE            K8s namespace for DFE apps (e.g. dfe-prod)
#   DFE_CLICKHOUSE_HOST      ClickHouse service hostname
#   DFE_KAFKA_BOOTSTRAP      Kafka bootstrap servers
#   DFE_KAFKA_PROVIDER       strimzi | redpanda | msk | confluent-cloud |
#                            redpanda-cloud; gates the otel-collector chart's
#                            MSK open_monitoring scrape. Optional -- empty on a
#                            deployment that predates this fact.
#   DFE_KAFKA_BOOTSTRAP_IAM  the SASL/IAM-authenticated endpoint the in-cluster
#                            MSK bootstrap Job connects to, from the aws root's
#                            managed-kafka/msk output. Empty on every provider
#                            but msk, and on an msk deployment predating this.
#   DFE_KAFKA_BOOTSTRAP_ROLE_ARN  the role the bootstrap Job's Pod Identity
#                            association names, from the same output. Carried
#                            onto the cluster secret for completeness; the Job
#                            itself needs no role ARN in its own pod spec --
#                            EKS Pod Identity resolves the credential by
#                            namespace + service account alone.
#   DFE_KAFKA_CREDENTIAL_REF where the broker's own copy of the SCRAM
#                            credential lives, from the same output. Carried
#                            onto the cluster secret for completeness; no
#                            current consumer reads it back.
#   DFE_OTEL_ENDPOINT        OTel Collector gRPC endpoint
#   DFE_VAULT_ADDR           OpenBao/Vault address (DFE_SECRETS_BACKEND=openbao)
#   DFE_VAULT_ROLE_ID        ESO AppRole role_id  (DFE_SECRETS_BACKEND=openbao)
#   DFE_WORKLOAD_IDENTITY_ANNOTATIONS  JSON map of service → cloud identity annotations
#   DFE_REGISTRY_HOST        JFrog registry hostname
#   DFE_REGISTRY_USER        JFrog service account username
#   DFE_REGISTRY_TOKEN       JFrog API token
#
# Optional:
#   DFE_SECRETS_BACKEND      which body the ESO ClusterSecretStore gets: openbao
#                            (default, needs DFE_VAULT_ADDR + DFE_VAULT_ROLE_ID
#                            and an AppRole SecretID) or aws-sm (needs none of
#                            them -- external-secrets authenticates as the pod
#                            it runs in, through EKS Pod Identity).
#   DFE_SECRETS_REGION       aws-sm only: the region the store reads from.
#   DFE_SECRETS_PREFIX       aws-sm only: the path every remoteRef hangs off.
#   DFE_BASE_DOMAIN          estate domain the profile tag is prefixed to when
#                            DFE_DOMAIN is unset (one cluster, one profile at a
#                            time, one set of hostnames per profile)
#   DFE_GATEWAY_IP           address the Envoy Gateway's LoadBalancer must take;
#                            empty lets the pool choose
#   DFE_RECEIVER_IP          address the receiver's public TCP LoadBalancer must
#                            take; empty lets the pool choose
#   DFE_KUBE_CLUSTER_NAME    the EKS cluster's own name, for the AWS Load
#                            Balancer Controller appset's cluster_name
#                            annotation; empty omits the annotation (non-EKS
#                            clouds)
#   DFE_KARPENTER_DISCOVERY_TAG      the karpenter.sh/discovery tag value, for
#                            karpenter-pools' karpenter.cluster.discoveryTag;
#                            empty omits the annotation (non-AWS clouds)
#   DFE_KARPENTER_INSTANCE_PROFILE   the pre-provisioned node instance profile,
#                            for karpenter-pools' karpenter.cluster.instanceProfile;
#                            empty omits the annotation (non-AWS clouds)
#   DFE_KARPENTER_KMS_KEY_ID the deployment CMK every node root volume is
#                            encrypted with, for
#                            karpenter-pools' karpenter.cluster.kmsKeyId; empty
#                            omits the annotation (non-AWS clouds)
#   DFE_KAFKA_BROKER_HOSTS   derived, not set by the caller: every broker's bare
#                            host (no port), comma separated, from
#                            DFE_KAFKA_BOOTSTRAP. Empty on every provider but msk.
#   DFE_TELEMETRY_SINK       otel (default) | cloudwatch, from the cloud root's
#                            telemetry dial. On DFE_CLOUD=aws with otel, renders
#                            the fetcher's AWS telemetry pre-config (below);
#                            empty or cloudwatch renders nothing here.
#   DFE_KAFKA_BROKER_LOG_BUCKET  the S3 bucket MSK's broker logs land in under
#                            the otel sink; empty on every other provider or sink.
#   DFE_CLOUDTRAIL_BUCKET    the S3 bucket the aws root's CloudTrail delivers to.
#   DFE_EKS_AUDIT_LOG_GROUP  the CloudWatch log group EKS's control-plane audit
#                            stream lands in -- unavoidable under either sink.
#   DFE_LOCAL_PATH_DIR       directory on each node local-path-provisioner creates
#                            its volumes under, when the bootstrap installs it
#                            because the cluster has no StorageClass. Unset keeps
#                            upstream's /opt/local-path-provisioner, on the root
#                            filesystem of a node whose data disk is elsewhere.
#   DFE_CLICKHOUSE_DEFAULT_TTL_DAYS  days every time-series table keeps rows, the
#                            OTel tables included (default 90; 0 = no default
#                            TTL). A source or a dfe-schemas definition with its
#                            own TTL overrides it.
#   DFE_CERTMANAGER_SECRET_ID  AppRole SecretID cert-manager authenticates to the
#                            estate Vault/OpenBao PKI with, for the gateway
#                            chart's tls.vault issuer mode. Set it and the edge
#                            certificate chains to a root every client already
#                            trusts; leave it unset and the deploy signs the edge
#                            with its own private root.
#   DFE_CA_PERSIST           true|false (default: true when DFE_VAULT_SECRET_ID
#                            is set) -- save the private root to the deployment's
#                            secret store and restore it on the next bootstrap,
#                            so a rebuild reuses it and no client re-trusts.
#   DFE_CA_SECRET_STORE      ClusterSecretStore the root is saved to and restored
#                            from (default dfe-secret-store).
#   DFE_CA_RESTORE_TIMEOUT   seconds to wait for the restore (default 60).
#   DFE_SIZING_OVERRIDE=1    On-prem only (DFE_CLOUD=local): accept a cluster
#                            whose real nodes fall short of sizing/<profile>.nodes.json
#                            -- scripts/check_node_capacity.py prints the
#                            demanded-vs-allocatable table as a WARNING instead
#                            of refusing. The warning still stands: the first
#                            thing to give is the broker under peak.
#   DFE_DRY_RUN=true         Print commands without executing (for CI validation)
#   DFE_POST=full            Power-on self test run after the deploy converges:
#                              full       readiness gate + CORE e2e (default)
#                              readiness  readiness gate only (fast, read-only)
#                              off        neither -- deploy health NOT verified
#                            A production deploy should run 'full'. 'readiness'
#                            and 'off' suit previews, or a stand-up that runs the
#                            POST separately (bootstrap/run-all-smoke-tests.sh).
#                            Anything not run is reported as NOT verified.
#                            Legacy per-gate vars are still honoured and override
#                            DFE_POST for that gate:
#                            DFE_SKIP_READINESS_GATE / DFE_SKIP_INTEGRATION_TESTS
#   DFE_POST_CLEANUP=false   Delete the CORE-2 sample rows once the ingest path
#                            is proven. Default keep -- the themed rows double as
#                            a live HyperDX JSON test bed, scoped to the run marker.
#   DFE_POST_FIXTURE=<path>  NDJSON sample-event fixture posted by CORE 2
#                            (default bootstrap/fixtures/post-hitchhiker.ndjson).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="${SCRIPT_DIR}/templates"

# Detect Terraform or OpenTofu (informational only). bootstrap.sh itself never
# invokes the IaC tool -- terraform/tofu runs EARLIER (the apply), and its outputs
# reach us as DFE_* env vars via bridge.py. So a missing binary is not fatal here:
# a deployer driving bootstrap from a pre-computed .env need not have it on PATH.
TF="$(command -v tofu || command -v terraform || true)"
if [[ -n "${TF}" ]]; then
  echo "Detected IaC tool: ${TF}"
else
  echo "No terraform/opentofu on PATH -- continuing (bootstrap.sh does not invoke it)."
fi

# Read versions from SSOT (versions.yaml)
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CERT_MANAGER_VERSION=$(python3 "${SCRIPT_DIR}/read_versions.py" --file "${REPO_ROOT}/versions.yaml" bootstrap.cert-manager)
EXTERNAL_SECRETS_VERSION=$(python3 "${SCRIPT_DIR}/read_versions.py" --file "${REPO_ROOT}/versions.yaml" bootstrap.external-secrets)
ARGOCD_VERSION=$(python3 "${SCRIPT_DIR}/read_versions.py" --file "${REPO_ROOT}/versions.yaml" bootstrap.argocd)
LOCAL_PATH_VERSION=$(python3 "${SCRIPT_DIR}/read_versions.py" --file "${REPO_ROOT}/versions.yaml" bootstrap.local-path-provisioner)
METALLB_VERSION=$(python3 "${SCRIPT_DIR}/read_versions.py" --file "${REPO_ROOT}/versions.yaml" bootstrap.metallb)
echo "Versions (from versions.yaml): cert-manager=${CERT_MANAGER_VERSION} eso=${EXTERNAL_SECRETS_VERSION} argocd=${ARGOCD_VERSION} local-path=${LOCAL_PATH_VERSION} metallb=${METALLB_VERSION}"

# Each operator below states its own Kubernetes window, so an under-floor cluster
# fails inside one of them naming that operator rather than the cluster.
# DFE_SKIP_PLATFORM_CHECK=true proceeds anyway.
if [[ "${DFE_SKIP_PLATFORM_CHECK:-false}" == "true" ]]; then
  echo "WARNING: DFE_SKIP_PLATFORM_CHECK=true -- not checking the cluster against platform.kubernetes" >&2
else
  # dfe-ops names the stack it is deploying, so check the floor of THAT stack
  # rather than whatever `current` happens to point at.
  platform_args=(--file "${REPO_ROOT}/versions.yaml")
  if [[ -n "${DFE_STACK_VERSION:-}" ]]; then
    platform_args+=(--stack "${DFE_STACK_VERSION}")
  fi
  python3 "${SCRIPT_DIR}/check_platform.py" "${platform_args[@]}"
fi

# Dry-run wrapper
run() {
  if [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
    echo "[DRY-RUN] $*"
  else
    "$@"
  fi
}

# Detect-or-install helpers. DFE assumes only a bare cluster and brings what it
# needs -- but a destination may already run a cluster-singleton operator
# (cert-manager, ESO, Argo) whose cluster-scoped CRDs cannot be owned twice. So
# each install is gated: CRD already present -> ADOPT the existing operator (skip
# install, reuse its CRDs); absent -> INSTALL DFE-owned. Logs the decision (never
# a silent skip). DFE_FORCE_INSTALL=true overrides (always install).
# NOTE: the Argo-managed operators in argocd/appsets (external-dns, keda,
# metrics-server, reloader, cnpg) are NOT yet guarded -- per-operator appset
# adopt is a later iteration that needs a rich cluster to validate. On a bare
# cluster they install correctly.
dfe_have_crd() { kubectl get crd "$1" >/dev/null 2>&1; }

# dfe_should_install <name> <crd> [<ns> <deploy>]  -> rc 0 = INSTALL, rc 1 = ADOPT.
# ADOPT only when BOTH the CRD and a running operator deployment are present. A
# LINGERING CRD with no deployment (e.g. left behind by destroy.sh's resource-policy
# keep) must NOT fool us into skipping the install -- that stranded argocd +
# cert-manager (uninstalled but CRDs kept) on a clean-slate redeploy. So: CRD +
# deployment -> ADOPT; CRD but no deployment -> INSTALL (re-adopts the CRD).
dfe_should_install() {
  local name="$1" crd="$2" ns="${3:-}" deploy="${4:-}"
  if [[ "${DFE_FORCE_INSTALL:-false}" == "true" ]]; then
    echo "  [${name}] DFE_FORCE_INSTALL -> INSTALL DFE-owned"
    return 0
  fi
  if dfe_have_crd "${crd}"; then
    if [[ -n "${ns}" && -n "${deploy}" ]] && ! kubectl -n "${ns}" get deploy "${deploy}" >/dev/null 2>&1; then
      echo "  [${name}] CRD ${crd} present but ${ns}/${deploy} not running (lingering CRD) -> INSTALL"
      return 0
    fi
    echo "  [${name}] detected (CRD ${crd} + running operator) -> ADOPT existing, skip install"
    return 1
  fi
  echo "  [${name}] not detected -> INSTALL DFE-owned"
  return 0
}

# The clouds whose own controller programs a LoadBalancer Service. Everything
# else is on-prem, whatever a deployment calls itself -- local, local-dfe, an
# estate name -- so the list is the clouds, not the on-prem names. dfe-ops
# preflight reads this same line so its INSTALL preview matches step [3b/7];
# scripts/tests/test_pinned_addresses.py holds the two together.
DFE_CLOUD_LB_PROVIDERS="aws gcp azure"

dfe_cloud_programs_loadbalancers() {
  [[ " ${DFE_CLOUD_LB_PROVIDERS} " == *" ${DFE_CLOUD} "* ]]
}

# Which secret store this deployment reads. openbao stays the default, so a
# deployment that names no backend behaves exactly as it did.
export DFE_SECRETS_BACKEND="${DFE_SECRETS_BACKEND:-openbao}"
case "${DFE_SECRETS_BACKEND}" in
  openbao|aws-sm) ;;
  *)
    echo "ERROR: DFE_SECRETS_BACKEND must be openbao or aws-sm (got '${DFE_SECRETS_BACKEND}')" >&2
    exit 1
    ;;
esac
# The store lives in the deployment's own region unless it is told otherwise.
export DFE_SECRETS_REGION="${DFE_SECRETS_REGION:-${DFE_REGION:-}}"
# The store prepends this to every remoteRef, and ESO adds no separator of its
# own. Empty leaves keys absolute.
DFE_SECRETS_PREFIX_PATH="${DFE_SECRETS_PREFIX:+${DFE_SECRETS_PREFIX%/}/}"
export DFE_SECRETS_PREFIX_PATH

# Validate required variables
required_vars=(
  DFE_ENV DFE_CLOUD DFE_REGION DFE_DOMAIN DFE_PROFILE
  DFE_REPO_URL DFE_TARGET_REVISION
  DFE_STORAGE_CLASS DFE_NAMESPACE
  DFE_CLICKHOUSE_HOST DFE_OTEL_ENDPOINT
  DFE_WORKLOAD_IDENTITY_ANNOTATIONS
)
if [[ "${DFE_SECRETS_BACKEND}" == "openbao" ]]; then
  required_vars+=(DFE_VAULT_ADDR DFE_VAULT_ROLE_ID)
else
  required_vars+=(DFE_SECRETS_REGION)
fi
# DFE_KAFKA_BOOTSTRAP is OPTIONAL: the slim and mesh profiles are gRPC (kafka
# disabled), so it is empty there; only set when kafka.mode != disabled. Defaulted
# empty so the cluster-secret annotation renders blank (kafka-dependent apps are
# gated off on a brokerless profile anyway).
export DFE_KAFKA_BOOTSTRAP="${DFE_KAFKA_BOOTSTRAP:-}"
# Who runs the brokers -- the last path segment of the credential's store key,
# and (below) what gates the otel-collector chart's MSK open_monitoring scrape.
# Empty on a deployment that predates this fact; the appset defaults it to
# strimzi, the kafka chart's own default.
export DFE_KAFKA_PROVIDER="${DFE_KAFKA_PROVIDER:-}"
# The bare host list a Prometheus scrape target needs (no port, no SASL
# scheme) -- MSK's own bootstrap string already lists every broker, comma
# separated, which the kafkametrics receiver's single seed broker does not
# expose on its own. Empty on every provider but msk.
DFE_KAFKA_BROKER_HOSTS=""
if [[ -n "${DFE_KAFKA_BOOTSTRAP}" ]]; then
  IFS=',' read -ra _dfe_bootstrap_entries <<< "${DFE_KAFKA_BOOTSTRAP}"
  _dfe_broker_hosts=()
  for _dfe_entry in "${_dfe_bootstrap_entries[@]}"; do
    _dfe_broker_hosts+=("${_dfe_entry%%:*}")
  done
  DFE_KAFKA_BROKER_HOSTS=$(IFS=,; echo "${_dfe_broker_hosts[*]}")
  unset _dfe_bootstrap_entries _dfe_broker_hosts _dfe_entry
fi
export DFE_KAFKA_BROKER_HOSTS
# The IAM bootstrap endpoint, its Pod Identity role and the broker's own
# credential reference -- tofu outputs on an msk deployment, empty everywhere
# else. Defaulted the same way as DFE_KAFKA_BOOTSTRAP/DFE_KAFKA_PROVIDER above,
# so the cluster-secret annotations below always render rather than reference
# an unset variable.
export DFE_KAFKA_BOOTSTRAP_IAM="${DFE_KAFKA_BOOTSTRAP_IAM:-}"
export DFE_KAFKA_BOOTSTRAP_ROLE_ARN="${DFE_KAFKA_BOOTSTRAP_ROLE_ARN:-}"
export DFE_KAFKA_CREDENTIAL_REF="${DFE_KAFKA_CREDENTIAL_REF:-}"
# kafka.mode flips to "external" only for a managed broker (msk,
# confluent-cloud, redpanda-cloud) -- strimzi and redpanda run in-cluster and
# take their mode from the profile overlay (profile-*.yaml), which this must
# NEVER override. Empty means "leave the profile's own kafka.mode alone": the
# appset parameter that reads the resulting annotation is emitted only when it
# is non-empty, for exactly this reason (argocd/appsets/layer2-data.yaml).
DFE_KAFKA_MODE=""
case "${DFE_KAFKA_PROVIDER}" in
  msk|confluent-cloud|redpanda-cloud) DFE_KAFKA_MODE="external" ;;
esac
export DFE_KAFKA_MODE
# external-dns provider name (aws, google, azure, cloudflare, rfc2136, ...);
# "none" deploys no external-dns, because its own default provider is aws and an
# uncredentialled install crash-loops against Route 53 forever (#223).
export DFE_DNS_PROVIDER="${DFE_DNS_PROVIDER:-none}"
# devex/local enforces DFE onto its dedicated workers via a HARD nodeSelector
# (argocd/values/local.yaml). Label the nodes by default there so the selector is
# satisfiable; a shared/customer cluster labels its own nodes at provisioning.
# Deliberately NARROWER than dfe_cloud_programs_loadbalancers: that one asks who
# programs a LoadBalancer, this one asks whether every node in the cluster is
# ours to label, and on a shared on-prem cluster it is not.
DFE_LABEL_WORKLOAD_NODES="${DFE_LABEL_WORKLOAD_NODES:-$([[ "${DFE_CLOUD:-}" == "local" ]] && echo true || echo false)}"
# The certified stack version; empty when a bare bootstrap names none, and the
# engine then falls back to the deploy repo's pins.
export DFE_STACK_VERSION="${DFE_STACK_VERSION:-}"
# Front-door addresses; empty renders a blank annotation and the pool chooses.
export DFE_GATEWAY_IP="${DFE_GATEWAY_IP:-}"
export DFE_RECEIVER_IP="${DFE_RECEIVER_IP:-}"
# DFE's monitoring goes to its own OTel feed and HyperDX, never CloudWatch --
# otel is the default sink a cloud root renders, and cloudwatch is the opt-in
# AWS-native path. Empty (a non-cloud caller, or one with nothing to report)
# renders no fetcher pre-config step below, the same as any other unset
# optional fact.
export DFE_TELEMETRY_SINK="${DFE_TELEMETRY_SINK:-}"
export DFE_KAFKA_BROKER_LOG_BUCKET="${DFE_KAFKA_BROKER_LOG_BUCKET:-}"
export DFE_CLOUDTRAIL_BUCKET="${DFE_CLOUDTRAIL_BUCKET:-}"
export DFE_EKS_AUDIT_LOG_GROUP="${DFE_EKS_AUDIT_LOG_GROUP:-}"
# The EKS cluster name for the LBC appset's cluster_name annotation, rendered only when set.
export DFE_KUBE_CLUSTER_NAME="${DFE_KUBE_CLUSTER_NAME:-}"
export DFE_KUBE_CLUSTER_NAME_ANNOTATION="${DFE_KUBE_CLUSTER_NAME:+dfe.hyperi.io/cluster_name: \"${DFE_KUBE_CLUSTER_NAME}\"}"
# The karpenter-pools chart's three cluster facts, each rendered only when set --
# empty on a non-AWS cloud, where Karpenter does not run.
export DFE_KARPENTER_DISCOVERY_TAG="${DFE_KARPENTER_DISCOVERY_TAG:-}"
export DFE_KARPENTER_DISCOVERY_TAG_ANNOTATION="${DFE_KARPENTER_DISCOVERY_TAG:+dfe.hyperi.io/karpenter_discovery_tag: \"${DFE_KARPENTER_DISCOVERY_TAG}\"}"
export DFE_KARPENTER_INSTANCE_PROFILE="${DFE_KARPENTER_INSTANCE_PROFILE:-}"
export DFE_KARPENTER_INSTANCE_PROFILE_ANNOTATION="${DFE_KARPENTER_INSTANCE_PROFILE:+dfe.hyperi.io/karpenter_instance_profile: \"${DFE_KARPENTER_INSTANCE_PROFILE}\"}"
export DFE_KARPENTER_KMS_KEY_ID="${DFE_KARPENTER_KMS_KEY_ID:-}"
export DFE_KARPENTER_KMS_KEY_ID_ANNOTATION="${DFE_KARPENTER_KMS_KEY_ID:+dfe.hyperi.io/karpenter_kms_key_id: \"${DFE_KARPENTER_KMS_KEY_ID}\"}"
# The in-cluster toolbox pod's dial facts (deployment.yaml's toolbox.pod.*,
# render_dial.py's DFE_TOOLBOX_POD_* keys). Unlike the karpenter facts above,
# these are not conditional on AWS -- the chart deploys on every cloud -- so
# they always render, defaulted to the chart's own off state rather than
# omitted, and carried straight through as the literal text a real YAML
# parser reads as a boolean (or an empty/numeric string for ttlSeconds).
export DFE_TOOLBOX_POD_ENABLED="${DFE_TOOLBOX_POD_ENABLED:-false}"
export DFE_TOOLBOX_POD_KUBE_API_ACCESS="${DFE_TOOLBOX_POD_KUBE_API_ACCESS:-false}"
export DFE_TOOLBOX_POD_TTL_SECONDS="${DFE_TOOLBOX_POD_TTL_SECONDS:-}"
# Deployment-wide retention, defaulted so the annotation always renders and the
# operator sees the value this deploy commits to. Whole days; 0 = no default TTL.
export DFE_CLICKHOUSE_DEFAULT_TTL_DAYS="${DFE_CLICKHOUSE_DEFAULT_TTL_DAYS:-90}"
if ! [[ "${DFE_CLICKHOUSE_DEFAULT_TTL_DAYS}" =~ ^[0-9]+$ ]]; then
  echo "ERROR: DFE_CLICKHOUSE_DEFAULT_TTL_DAYS must be a whole number of days (got '${DFE_CLICKHOUSE_DEFAULT_TTL_DAYS}')" >&2
  exit 1
fi
echo "Default retention: ${DFE_CLICKHOUSE_DEFAULT_TTL_DAYS} day(s) for every time-series table (DFE_CLICKHOUSE_DEFAULT_TTL_DAYS; 0 = none)"
# One cluster runs one profile at a time, so the profile tags the domain and no
# two deployments publish the same hostname. dfe-ops refuses an explicit
# DFE_DOMAIN that contradicts a declared base; a bare bootstrap trusts it.
# A Kubernetes deploy that names no profile gets the HA tier on the bus.
export DFE_PROFILE="${DFE_PROFILE:-scale}"
if [[ -z "${DFE_DOMAIN:-}" && -n "${DFE_BASE_DOMAIN:-}" ]]; then
  export DFE_DOMAIN="${DFE_PROFILE}.${DFE_BASE_DOMAIN}"
  echo "Domain derived from DFE_BASE_DOMAIN: ${DFE_DOMAIN}"
fi
# Registry vars are optional — skip regcred if not set
# DFE_REGISTRY_HOST DFE_REGISTRY_USER DFE_REGISTRY_TOKEN
missing=()
for var in "${required_vars[@]}"; do
  [[ -z "${!var:-}" ]] && missing+=("$var")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "ERROR: Missing required environment variables: ${missing[*]}" >&2
  exit 1
fi

# Every appset layers argocd/values/<cloud>.yaml over common.yaml, so a cloud
# with no overlay deploys chart defaults and says nothing (dfe-infra#130).
DFE_CLOUD_VALUES="${REPO_ROOT}/argocd/values/${DFE_CLOUD}.yaml"
if [[ ! -f "${DFE_CLOUD_VALUES}" ]]; then
  echo "ERROR: DFE_CLOUD=${DFE_CLOUD} has no overlay at ${DFE_CLOUD_VALUES}" >&2
  echo "       Without it every chart takes its own defaults -- no storage class," >&2
  echo "       no Service type, no per-cloud identity -- and nothing reports it." >&2
  echo "       Add the overlay, or correct DFE_CLOUD -- the ones that exist are the" >&2
  echo "       non-profile files in ${REPO_ROOT}/argocd/values/." >&2
  exit 1
fi
echo "Cloud overlay: ${DFE_CLOUD_VALUES}"

echo "==> [0a/7] On-prem node-capacity preflight"
# DFE_CLOUD=local is this script's own token for "we do not create these
# nodes" (see the header's DFE_CLOUD comment) -- the same substrate
# sizing/targets/onprem.yaml and compute-shapes.yaml's onprem stub call
# `cloud: onprem`. resolve_sizing.py writes the demand only for that cloud, so
# a nodes.json existing at all already means this deployment is on-prem; a
# cloud that creates its own nodes never gets one and this step is a no-op.
DFE_SIZING_NODES_FILE="${REPO_ROOT}/sizing/${DFE_PROFILE}.nodes.json"
if [[ "${DFE_CLOUD}" == "local" && -f "${DFE_SIZING_NODES_FILE}" ]]; then
  echo "  Checking ${DFE_SIZING_NODES_FILE} against the cluster's real nodes..."
  if ! python3 "${SCRIPT_DIR}/../scripts/check_node_capacity.py" \
      --nodes-file "${DFE_SIZING_NODES_FILE}" \
      ${KUBECONFIG:+--kubeconfig "${KUBECONFIG}"}; then
    echo "ERROR: the cluster's real nodes fall short of the sizing demand -- see the table above." >&2
    echo "       Add capacity, correct the dial and re-resolve, or set DFE_SIZING_OVERRIDE=1 to" >&2
    echo "       accept it and continue (the warning still stands: the broker gives first)." >&2
    exit 1
  fi
elif [[ "${DFE_CLOUD}" == "local" ]]; then
  echo "  No ${DFE_SIZING_NODES_FILE} -- nothing to check, skipping"
else
  echo "  DFE_CLOUD=${DFE_CLOUD} creates its own nodes -- nothing to check, skipping"
fi

# Compute base64 auth for registry (only if registry vars are set)
if [[ -n "${DFE_REGISTRY_HOST:-}" ]] && [[ -n "${DFE_REGISTRY_USER:-}" ]]; then
  export DFE_REGISTRY_AUTH
  DFE_REGISTRY_AUTH=$(printf '%s:%s' "${DFE_REGISTRY_USER}" "${DFE_REGISTRY_TOKEN}" | base64)
fi

# Deploy repo (dfe-engine writes config, Argo watches). PROVIDER-AGNOSTIC seam:
# external git (GitHub ~85% / GitLab ~10%) is PRIMARY; the in-cluster Forgejo
# fallback (~5%, tyre-kicking/air-gapped) is used ONLY when no external URL is
# given. If DFE_CONFIG_REPO_URL is set -> external mode (deployer also supplies
# creds, see [4d] below); unset -> bundled Forgejo fallback.
export DFE_CONFIG_REPO_REVISION="${DFE_CONFIG_REPO_REVISION:-main}"
# Admin user owns the bundled deploy repo (fallback path only). The engine's
# DFE_GITOPS_REPO_URL is injected from the config_repo_url annotation by the
# layer2-apps appset, so it tracks this URL automatically -- no manual
# owner-matching to keep in sync, and the engine cannot write a different repo
# than Argo reads.
FORGEJO_ADMIN_USER="${DFE_FORGEJO_ADMIN_USER:-dfe-admin}"
if [[ -n "${DFE_CONFIG_REPO_URL:-}" ]]; then
  export DFE_BUNDLED_DEPLOY_REPO="false"
  echo "Deploy repo: EXTERNAL git (${DFE_CONFIG_REPO_URL}) -- no in-cluster server."
else
  export DFE_CONFIG_REPO_URL="http://dfe-forgejo.forgejo.svc.cluster.local:3000/${FORGEJO_ADMIN_USER}/deploy.git"
  export DFE_BUNDLED_DEPLOY_REPO="true"
  echo "Deploy repo: FALLBACK in-cluster Forgejo (no external git supplied)."
fi

# Add Helm repos (idempotent)
echo "==> [0/7] Adding Helm repositories"
run helm repo add jetstack https://charts.jetstack.io 2>/dev/null || true
run helm repo add external-secrets https://charts.external-secrets.io 2>/dev/null || true
run helm repo add argo https://argoproj.github.io/argo-helm 2>/dev/null || true
run helm repo add metallb https://metallb.github.io/metallb 2>/dev/null || true
run helm repo update

echo "==> [1/7] Applying ArgoCD namespace + cluster secret"
if [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
  echo "[DRY-RUN] kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -"
  echo "[DRY-RUN] envsubst < ${TEMPLATES_DIR}/cluster-secret.yaml.tpl | kubectl apply -f -"
else
  kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
  envsubst < "${TEMPLATES_DIR}/cluster-secret.yaml.tpl" | kubectl apply -f -
fi

echo "==> [1b/7] StorageClass (detect-or-install)"
# DFE assumes only a bare cluster. If a default StorageClass exists -> ADOPT it.
# If StorageClasses exist but none is default -> use DFE_STORAGE_CLASS as-is. If
# NONE exist (bare RKE2/EKS) -> INSTALL local-path-provisioner (pinned) and mark
# it default, so the substrate's PVCs (CH/Gitea/CNPG) can bind with no deployer
# input. This is the onboarding-contract storage derive.
if kubectl get storageclass -o jsonpath='{range .items[*]}{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}{"\n"}{end}' 2>/dev/null | grep -q true; then
  echo "  default StorageClass present -> ADOPT"
elif [[ -n "$(kubectl get storageclass -o name 2>/dev/null)" ]]; then
  echo "  StorageClass(es) present, none default -> using DFE_STORAGE_CLASS=${DFE_STORAGE_CLASS}"
else
  echo "  no StorageClass -> INSTALL local-path-provisioner ${LOCAL_PATH_VERSION}"
  run kubectl apply -f "https://raw.githubusercontent.com/rancher/local-path-provisioner/${LOCAL_PATH_VERSION}/deploy/local-path-storage.yaml"
  run kubectl -n local-path-storage rollout status deployment/local-path-provisioner --timeout=120s
  run kubectl patch storageclass local-path -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
  # Upstream hands every node /opt/local-path-provisioner, so on a node whose data
  # disk is mounted elsewhere every PV lands on the root filesystem.
  if [[ -n "${DFE_LOCAL_PATH_DIR:-}" ]]; then
    echo "  local-path volumes -> ${DFE_LOCAL_PATH_DIR}"
    if [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
      echo "[DRY-RUN] patch local-path-config config.json nodePathMap -> ${DFE_LOCAL_PATH_DIR}"
    else
      local_path_config="$(kubectl -n local-path-storage get configmap local-path-config \
        -o jsonpath='{.data.config\.json}' \
        | python3 "${SCRIPT_DIR}/local_path_dir.py" --dir "${DFE_LOCAL_PATH_DIR}")"
      kubectl -n local-path-storage patch configmap local-path-config \
        --type merge -p "$(python3 -c 'import json,sys; print(json.dumps({"data": {"config.json": sys.stdin.read()}}))' <<<"${local_path_config}")"
      # The provisioner reads config.json at start; a running pod keeps the old path.
      kubectl -n local-path-storage rollout restart deployment/local-path-provisioner
      kubectl -n local-path-storage rollout status deployment/local-path-provisioner --timeout=120s
    fi
  fi
fi

echo "==> [1c/7] Node labels (dedicated-worker placement)"
# When a HARD nodeSelector is in play (devex/local) the target nodes MUST carry the
# dfe.hyperi.io/workload=dfe label or the data pods sit Pending forever. Label here
# as Layer-0 node-prep. Opt-in via DFE_LABEL_WORKLOAD_NODES (default on for local).
if [[ "${DFE_LABEL_WORKLOAD_NODES}" == "true" ]]; then
  run kubectl label nodes --all dfe.hyperi.io/workload=dfe --overwrite
  echo "  Labelled all nodes dfe.hyperi.io/workload=dfe"
else
  echo "  Node labelling skipped (DFE_LABEL_WORKLOAD_NODES=false) -- soft/no placement"
fi

echo "==> [2/7] cert-manager (detect-or-install)"
if dfe_should_install cert-manager certificates.cert-manager.io cert-manager cert-manager; then
  # enableGatewayAPI: the gateway-shim watches Gateway annotations and issues
  # the dfe-wildcard-tls Secret the https listener references; without it the
  # annotation is inert and the listener never programs.
  run helm upgrade --install cert-manager jetstack/cert-manager \
    --namespace cert-manager --create-namespace \
    --version "${CERT_MANAGER_VERSION}" \
    --set crds.enabled=true \
    --set config.enableGatewayAPI=true \
    --wait --timeout 5m
fi
# Private-CA issuance (the chart's tls.vault issuer mode): seed the AppRole
# SecretID cert-manager authenticates to Vault/OpenBao with. Outside the
# detect-or-install gate so an existing install still gets the secret.
if [[ -n "${DFE_CERTMANAGER_SECRET_ID:-}" ]] && [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  kubectl -n cert-manager create secret generic cert-manager-approle \
    --from-literal=secretId="${DFE_CERTMANAGER_SECRET_ID}" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "  Seeded cert-manager AppRole SecretID (cert-manager-approle)"
fi

echo "==> [3/7] external-secrets (detect-or-install)"
if dfe_should_install external-secrets clustersecretstores.external-secrets.io external-secrets external-secrets; then
  run helm upgrade --install external-secrets external-secrets/external-secrets \
    --namespace external-secrets --create-namespace \
    --version "${EXTERNAL_SECRETS_VERSION}" \
    --wait --timeout 5m
fi

echo "==> [3b/7] MetalLB (detect-or-install, on-prem only)"
# Nothing programs a LoadBalancer Service on a bare on-prem cluster, so the
# Envoy Gateway and the receiver's public door sit Pending forever.
if dfe_cloud_programs_loadbalancers; then
  echo "  DFE_CLOUD=${DFE_CLOUD}: the cloud LoadBalancer controller programs the Services -- MetalLB skipped"
else
  if dfe_should_install metallb ipaddresspools.metallb.io metallb-system metallb-controller; then
    run helm upgrade --install metallb metallb/metallb \
      --namespace metallb-system --create-namespace \
      --version "${METALLB_VERSION}" \
      --wait --timeout 5m
    # The IPAddressPool webhook is failurePolicy=Fail, so the pool below is
    # rejected until the controller serves it, and the speaker is what answers
    # ARP for the addresses once it is accepted.
    run kubectl -n metallb-system rollout status deployment/metallb-controller --timeout=300s
    run kubectl -n metallb-system rollout status daemonset/metallb-speaker --timeout=300s
  fi
  # Applied on every on-prem run, so a rebuild that adopts MetalLB still gets
  # the addresses this deployment's DNS records point at.
  if [[ -z "${DFE_GATEWAY_IP}" ]] || [[ -z "${DFE_RECEIVER_IP}" ]]; then
    echo "  WARNING: DFE_GATEWAY_IP and/or DFE_RECEIVER_IP are unset, so no address pool was created."
    echo "           MetalLB hands out nothing it holds no pool for: the Envoy Gateway and the"
    echo "           receiver's public Service stay Pending and every published hostname fails to"
    echo "           resolve to a live address. Set both and re-run, unless the cluster already"
    echo "           carried a LoadBalancer provider with a pool of its own."
  elif [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
    echo "[DRY-RUN] envsubst < ${TEMPLATES_DIR}/metallb-pool.yaml.tpl | kubectl apply -f -"
  else
    # A provider that came with the cluster already owns its addressing, and
    # MetalLB refuses a pool whose range overlaps one it is already serving.
    pools=$(kubectl get ipaddresspools.metallb.io -A -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)
    if [[ -n "${pools}" ]] && [[ " ${pools} " != *" dfe-front-door "* ]]; then
      echo "  Existing IPAddressPool(s) own this cluster's addressing (${pools}) -> DFE pool NOT applied."
      echo "  The gateway and receiver addresses must fall inside one of them."
    else
      envsubst < "${TEMPLATES_DIR}/metallb-pool.yaml.tpl" | kubectl apply -f -
      echo "  Applied the dfe-front-door IPAddressPool + L2Advertisement"
    fi
  fi
fi

echo "==> [4/7] ESO ClusterSecretStore (backend: ${DFE_SECRETS_BACKEND})"
# One store name, dfe-secret-store, whatever backs it -- every ExternalSecret in
# the tree references that name and none of them knows which cloud it is on.
if [[ "${DFE_SECRETS_BACKEND}" == "aws-sm" ]]; then
  ESO_STORE_TEMPLATE="${TEMPLATES_DIR}/eso-cluster-secret-store-aws.yaml.tpl"
else
  ESO_STORE_TEMPLATE="${TEMPLATES_DIR}/eso-cluster-secret-store.yaml.tpl"
fi
if [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
  if [[ "${DFE_SECRETS_BACKEND}" == "aws-sm" ]]; then
    echo "[DRY-RUN] envsubst ${ESO_STORE_TEMPLATE##*/} | kubectl apply -f -"
    echo "[DRY-RUN]   region=${DFE_SECRETS_REGION} prefix=${DFE_SECRETS_PREFIX_PATH:-<none>} auth=pod identity"
  else
    echo "[DRY-RUN] seed dfe-vault-approle-secret + envsubst store + patch caBundle"
  fi
elif [[ "${DFE_SECRETS_BACKEND}" == "aws-sm" ]]; then
  # No SecretID and no CA patch: the store carries no auth block, so ESO falls
  # through to the pod's own credentials, and Secrets Manager is a public AWS
  # endpoint whose certificate the SDK already trusts.
  envsubst < "${ESO_STORE_TEMPLATE}" | kubectl apply -f -
  echo "  ClusterSecretStore dfe-secret-store -> Secrets Manager in ${DFE_SECRETS_REGION}"
else
  # Seed the AppRole SecretID the store references. Nothing else creates it, so ESO
  # could never authenticate to OpenBao (store stuck InvalidProviderConfig).
  if [[ -n "${DFE_VAULT_SECRET_ID:-}" ]]; then
    kubectl create namespace external-secrets --dry-run=client -o yaml | kubectl apply -f -
    kubectl -n external-secrets create secret generic dfe-vault-approle-secret \
      --from-literal=roleSecretID="${DFE_VAULT_SECRET_ID}" \
      --dry-run=client -o yaml | kubectl apply -f -
    echo "  Seeded ESO AppRole SecretID (dfe-vault-approle-secret)"
  else
    echo "  WARNING: DFE_VAULT_SECRET_ID is unset, so dfe-vault-approle-secret was NOT created."
    echo "           ESO cannot authenticate to OpenBao, so every ExternalSecret stays unresolved."
    echo "           Downstream: dfe-local/dfe-kafka-user never appears and the data plane sits in"
    echo "           CreateContainerConfigError. Set DFE_VAULT_SECRET_ID and re-run."
  fi
  envsubst < "${ESO_STORE_TEMPLATE}" | kubectl apply -f -
  # ESO's vault provider cannot skip TLS verify (the env's VAULT_SKIP_VERIFY is for
  # terraform, which ESO ignores). Fetch OpenBao's issuing CA from its TLS handshake
  # and patch it into the store so it can verify the cert. No-op if none is found.
  if [[ -z "${DFE_VAULT_CA_BUNDLE:-}" ]] && [[ -n "${DFE_VAULT_ADDR:-}" ]]; then
    DFE_VAULT_CA_BUNDLE=$(echo | openssl s_client -connect "${DFE_VAULT_ADDR#*://}" -showcerts 2>/dev/null | python3 -c "import sys,re,base64; c=re.findall(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', sys.stdin.read(), re.S); sys.stdout.write(base64.b64encode(chr(10).join(c[1:]).encode()).decode() if len(c)>1 else '')" || true)
  fi
  if [[ -n "${DFE_VAULT_CA_BUNDLE:-}" ]]; then
    kubectl patch clustersecretstore dfe-secret-store --type merge \
      -p "{\"spec\":{\"provider\":{\"vault\":{\"caBundle\":\"${DFE_VAULT_CA_BUNDLE}\"}}}}"
    echo "  Patched OpenBao CA into the ESO store"
  fi
fi

echo "==> [4a/7] Internal CA root: restore from the secret store before cert-manager mints"
# A rebuild that mints a new root costs every client a re-trust, and the HyperDX
# iframe fails outright because it cannot show the interstitial (#238).
# Rendered from the gateway chart's ONE definition, ahead of Argo, so the restore
# lands before cert-manager sees the Certificate rather than racing it.
# cert-manager then adopts a root that already satisfies the spec.
# Without a secret store nothing can hold the root between rebuilds.
if [[ -z "${DFE_CA_PERSIST:-}" ]]; then
  # What decides it is whether the store can authenticate at all: on aws-sm the
  # pod's own identity is the credential, on openbao nothing works without the
  # AppRole SecretID and there is nowhere to hold the root.
  if [[ "${DFE_SECRETS_BACKEND}" != "openbao" || -n "${DFE_VAULT_SECRET_ID:-}" ]]; then
    DFE_CA_PERSIST="true"
  else
    DFE_CA_PERSIST="false"
  fi
fi
if [[ -n "${DFE_CERTMANAGER_SECRET_ID:-}" ]]; then
  echo "  Vault/OpenBao issuer mode seeded -- the estate PKI owns the root, nothing to persist"
elif [[ "${DFE_CA_PERSIST}" != "true" ]]; then
  echo "  SKIPPED (DFE_CA_PERSIST=${DFE_CA_PERSIST}): this deploy mints a fresh root and every"
  echo "  client must trust it again after a rebuild. Set DFE_VAULT_SECRET_ID so the deployment"
  echo "  has a working secret store, or DFE_CA_PERSIST=true to force it."
elif [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
  echo "[DRY-RUN] helm template envoy-gateway-config -s templates/internal-ca-persist.yaml | kubectl apply -f -"
else
  kubectl create namespace cert-manager --dry-run=client -o yaml | kubectl apply -f -
  helm template dfe-internal-ca "${REPO_ROOT}/helm/charts/envoy-gateway-config" \
    --namespace cert-manager \
    --show-only templates/internal-ca-persist.yaml \
    --set "env=${DFE_ENV}" \
    --set "cloud=${DFE_CLOUD}" \
    --set "tls.internalCA.persist.secretStoreName=${DFE_CA_SECRET_STORE:-dfe-secret-store}" \
    | kubectl apply -f -
  # A poll, not `kubectl wait --for=create`: that needs kubectl >= 1.31.
  # A first bootstrap has nothing to restore, so the timeout is expected.
  ca_deadline=$(( SECONDS + ${DFE_CA_RESTORE_TIMEOUT:-60} ))
  ca_restored=false
  while [[ "${SECONDS}" -lt "${ca_deadline}" ]]; do
    if kubectl -n cert-manager get secret dfe-internal-ca-tls >/dev/null 2>&1; then
      ca_restored=true
      break
    fi
    sleep 3
  done
  if [[ "${ca_restored}" == "true" ]]; then
    echo "  Root RESTORED from the secret store into cert-manager/dfe-internal-ca-tls"
  else
    echo "  No stored root (first bootstrap, or the store does not hold one yet):"
    echo "  cert-manager will mint one and the PushSecret will save it."
  fi
fi

echo "==> [4b/7] Creating imagePullSecrets"
# An imagePullSecret is namespace-scoped, so it goes in EVERY namespace that can
# pull a private DFE image -- not just the app namespace. The schema Job runs in
# `clickhouse` (where the admin credential lives) and pulls the engine image, so
# without this it only worked when an app deployment had already cached that tag
# on the node.
for ns in argocd "${DFE_NAMESPACE}" strimzi clickhouse otel hyperdx forgejo; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | run kubectl apply -f -
  # JFrog regcred (if registry credentials provided)
  if [[ -n "${DFE_REGISTRY_USER:-}" ]]; then
    TARGET_NAMESPACE="$ns" envsubst < "${TEMPLATES_DIR}/regcred.yaml.tpl" | run kubectl apply -f -
  fi
  # Private registry pull secret (GHCR, ECR, GCR, etc.)
  # Set DFE_PULL_SECRET_SERVER, DFE_PULL_SECRET_USER, DFE_PULL_SECRET_TOKEN in env.
  if [[ -n "${DFE_PULL_SECRET_TOKEN:-}" ]]; then
    kubectl -n "$ns" create secret docker-registry ghcr-pull-secret \
      --docker-server="${DFE_PULL_SECRET_SERVER:-ghcr.io}" \
      --docker-username="${DFE_PULL_SECRET_USER:-token}" \
      --docker-password="${DFE_PULL_SECRET_TOKEN}" \
      --dry-run=client -o yaml | run kubectl apply -f -
  fi
done
if [[ -n "${DFE_PULL_SECRET_TOKEN:-}" ]]; then
  echo "  Pull secret created/updated in every DFE namespace"
else
  echo "  WARNING: DFE_PULL_SECRET_TOKEN is unset, so ghcr-pull-secret was NOT created."
  echo "           Every app image from a private registry fails with ImagePullBackOff and"
  echo "           FailedToRetrieveImagePullSecret. Set DFE_PULL_SECRET_TOKEN and re-run."
fi

# [4c/7] Deploy-repo credentials -- two paths by provider mode.
if [[ "${DFE_BUNDLED_DEPLOY_REPO}" == "true" ]] && [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  # FALLBACK: in-cluster Forgejo. Generate (once) a random admin password, reuse
  # on re-runs. The Forgejo secret lives in the forgejo namespace (for Forgejo
  # itself); the engine's push cred goes into the dfe namespace under the
  # provider-agnostic name dfe-deploy-repo-auth (see [4c] external path -- SAME
  # name so the chart's credentialsSecret does not change between modes).
  echo "==> [4c/7] Ensuring Forgejo admin secret + engine deploy-repo write cred"
  if kubectl -n forgejo get secret dfe-forgejo-admin >/dev/null 2>&1; then
    FORGEJO_ADMIN_PASSWORD=$(kubectl -n forgejo get secret dfe-forgejo-admin -o jsonpath='{.data.password}' | base64 -d)
  else
    FORGEJO_ADMIN_PASSWORD=$(openssl rand -hex 24)
  fi
  kubectl create namespace forgejo --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n forgejo create secret generic dfe-forgejo-admin \
    --from-literal=username="${FORGEJO_ADMIN_USER}" \
    --from-literal=password="${FORGEJO_ADMIN_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl create namespace "${DFE_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n "${DFE_NAMESPACE}" create secret generic dfe-deploy-repo-auth \
    --from-literal=username="${FORGEJO_ADMIN_USER}" \
    --from-literal=password="${FORGEJO_ADMIN_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f -
  echo "  Forgejo admin secret (forgejo ns) + engine write cred dfe-deploy-repo-auth (${DFE_NAMESPACE}) ready"

  # The Forgejo -> Argo push webhook's shared secret. Forgejo signs the delivery
  # with it and Argo verifies against argocd-secret's webhook.gogs.secret, so
  # both ends carry the one value; minted once and reused, like the password
  # above. argocd-secret is PATCHED, never applied over: it also holds Argo's
  # server signing key and admin hash, adopted install or not.
  if kubectl -n forgejo get secret dfe-argo-webhook >/dev/null 2>&1; then
    ARGO_WEBHOOK_SECRET=$(kubectl -n forgejo get secret dfe-argo-webhook -o jsonpath='{.data.secret}' | base64 -d)
  else
    ARGO_WEBHOOK_SECRET=$(openssl rand -hex 24)
  fi
  kubectl -n forgejo create secret generic dfe-argo-webhook \
    --from-literal=secret="${ARGO_WEBHOOK_SECRET}" \
    --dry-run=client -o yaml | kubectl apply -f -
  if kubectl -n argocd patch secret argocd-secret --type merge \
      -p "{\"stringData\":{\"webhook.gogs.secret\":\"${ARGO_WEBHOOK_SECRET}\"}}" >/dev/null 2>&1; then
    echo "  Argo push webhook secret ready (forgejo ns + argocd-secret)"
  else
    echo "  WARNING: could not patch argocd-secret with webhook.gogs.secret."
    echo "           Forgejo will still register the hook, Argo will reject every"
    echo "           delivery, and a source write waits out the 300s poll instead."
  fi
elif [[ "${DFE_BUNDLED_DEPLOY_REPO}" != "true" ]] && [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  # EXTERNAL git (GitHub/GitLab/self-hosted): register the Argo READ credential so
  # Argo can pull the deploy repo -- EITHER HTTPS+token (DFE_CONFIG_REPO_USER +
  # DFE_CONFIG_REPO_TOKEN) OR an SSH key (DFE_CONFIG_REPO_SSH_KEY = path to a
  # private key). The engine's WRITE cred (dfe-deploy-repo-auth, dfe namespace) is
  # created below from the SAME token input -- same secret name as the bundled
  # path, so the dfe-engine chart is provider-agnostic.
  echo "==> [4c/7] Registering Argo repo cred for external deploy repo"
  if [[ -n "${DFE_CONFIG_REPO_SSH_KEY:-}" ]]; then
    kubectl -n argocd create secret generic repo-deploy \
      --from-literal=type=git \
      --from-literal=url="${DFE_CONFIG_REPO_URL}" \
      --from-file=sshPrivateKey="${DFE_CONFIG_REPO_SSH_KEY}" \
      --dry-run=client -o yaml | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | kubectl apply -f -
    echo "  Argo repo cred (SSH) registered for ${DFE_CONFIG_REPO_URL}"
  elif [[ -n "${DFE_CONFIG_REPO_TOKEN:-}" ]]; then
    kubectl -n argocd create secret generic repo-deploy \
      --from-literal=type=git \
      --from-literal=url="${DFE_CONFIG_REPO_URL}" \
      --from-literal=username="${DFE_CONFIG_REPO_USER:-oauth2}" \
      --from-literal=password="${DFE_CONFIG_REPO_TOKEN}" \
      --dry-run=client -o yaml | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | kubectl apply -f -
    echo "  Argo repo cred (HTTPS+token) registered for ${DFE_CONFIG_REPO_URL}"
  else
    echo "  WARNING: external deploy repo but no DFE_CONFIG_REPO_TOKEN/SSH_KEY -- Argo may not be able to pull it."
  fi
  # Engine WRITE cred, dfe namespace, provider-agnostic name (matches the bundled
  # path). The engine pushes over HTTPS+token (dulwich), so it needs a token even
  # when Argo READS via SSH. SSH-only external -> the engine cannot push; warn.
  if [[ -n "${DFE_CONFIG_REPO_TOKEN:-}" ]]; then
    kubectl create namespace "${DFE_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
    kubectl -n "${DFE_NAMESPACE}" create secret generic dfe-deploy-repo-auth \
      --from-literal=username="${DFE_CONFIG_REPO_USER:-oauth2}" \
      --from-literal=password="${DFE_CONFIG_REPO_TOKEN}" \
      --dry-run=client -o yaml | kubectl apply -f -
    echo "  Engine write cred dfe-deploy-repo-auth ready in ${DFE_NAMESPACE}"
  else
    echo "  WARNING: external deploy repo with no DFE_CONFIG_REPO_TOKEN -- the engine CANNOT push (gitcrud) over SSH; it needs an HTTPS token. Set DFE_CONFIG_REPO_TOKEN for engine writes."
  fi
fi

echo "==> [4d/7] Fetcher AWS telemetry pre-config (otel sink only)"
# DFE's monitoring goes to its own OTel feed, never CloudWatch, so on AWS with
# sink=otel the broker-log bucket, the CloudTrail trail and the EKS audit log
# have a fetcher-readable path instead. Under sink=cloudwatch these touchpoints
# already have an AWS-native destination, so nothing is rendered. A STARTER
# fragment, not a wired instance -- see the template's own header.
if [[ "${DFE_CLOUD}" == "aws" && "${DFE_TELEMETRY_SINK}" == "otel" ]]; then
  # `run` echoes instead of executing, so piping `run cmd-a | run cmd-b` under
  # DFE_DRY_RUN carries the left side's echo TEXT into the right side, which
  # also just echoes -- neither side ever sees real input. Guarded with an
  # `if`, like the ConfigMap apply two lines below, so a dry run prints one
  # readable line and a real run pipes for real.
  if [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
    echo "[DRY-RUN] kubectl create namespace ${DFE_NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -"
  else
    kubectl create namespace "${DFE_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
  fi
  if [[ "${DFE_DRY_RUN:-false}" == "true" ]]; then
    echo "[DRY-RUN] envsubst < ${TEMPLATES_DIR}/fetcher-aws-telemetry.yaml.tpl | kubectl apply -f -"
  else
    envsubst < "${TEMPLATES_DIR}/fetcher-aws-telemetry.yaml.tpl" | kubectl apply -f -
    echo "  ConfigMap dfe-fetcher-aws-telemetry-preconfig applied in ${DFE_NAMESPACE}"
  fi
else
  echo "  Skipped (DFE_CLOUD=${DFE_CLOUD}, DFE_TELEMETRY_SINK=${DFE_TELEMETRY_SINK:-<unset>}) -- otel on aws only"
fi

# Valkey for ArgoCD cache — check if already running, skip install if so.
# On fresh clusters: deploy plain Valkey manifest. On existing clusters: use existing.
VALKEY_SVC="${DFE_VALKEY_SERVICE:-valkey}"  # default: 'valkey' (hyperi-infra pattern)
echo "==> [5/7] Checking Valkey"
if kubectl -n argocd get svc "${VALKEY_SVC}" > /dev/null 2>&1; then
  echo "  Valkey service '${VALKEY_SVC}' already exists — skipping install"
else
  echo "  Deploying Valkey (ArgoCD cache) — plain manifest, no Bitnami"
  run kubectl apply -f "${TEMPLATES_DIR}/valkey.yaml"
  run kubectl -n argocd rollout status deployment/valkey --timeout=120s
fi

echo "==> [6/7] ArgoCD with Valkey cache (detect-or-install)"
# If the destination already runs Argo (its Application CRD + a server deploy are
# present) we ADOPT it -- our AppProjects/ApplicationSets below register into the
# existing Argo. Otherwise install DFE-owned Argo. (Full isolation -- a dedicated
# dfe-system Argo scoped to dfe-* namespaces so it never couples to a host Argo --
# is the Phase 0d adopt-path refinement.)
# Argo HARDENING (dfe-infra#4): back off the controller timers so a degraded app
# can never monopolise the control plane (self-heal 5s->30s, reconciliation
# 180s->300s) and bound the repo-server timeout. Mirrors the devex platform guard.
if dfe_should_install argocd applications.argoproj.io argocd argocd-server; then
  run helm upgrade --install argocd argo/argo-cd \
    --namespace argocd --create-namespace \
    --version "${ARGOCD_VERSION}" \
    --set redis.enabled=false \
    --set "externalRedis.host=${VALKEY_SVC}.argocd.svc.cluster.local" \
    --set "externalRedis.port=6379" \
    --set-string 'configs.params.reposerver\.disable\.git\.modules=true' \
    --set-string 'configs.cm.timeout\.reconciliation=300s' \
    --set-string 'configs.params.controller\.self\.heal\.timeout\.seconds=30' \
    --set-string 'configs.params.controller\.repo\.server\.timeout\.seconds=60' \
    --set-string 'configs.params.controller\.diff\.server\.side=true' \
    --wait --timeout 10m
else
  echo "  Using existing ArgoCD; registering DFE AppProjects + ApplicationSets into it."
fi

# Argo CD repo credential for the CHART repo (DFE_REPO_URL), which is where Argo
# READS charts -- distinct from the DEPLOY repo credential in [4c/7], where the
# engine WRITES. Optional, so one bootstrap serves both postures: a public chart
# repo needs no credential, a private one takes a token or an SSH key.
if [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  if [[ -n "${DFE_REPO_SSH_KEY:-}" ]] && [[ -f "${DFE_REPO_SSH_KEY}" ]]; then
    echo "  [chart repo] SSH key supplied -> creating Argo repository credential"
    kubectl -n argocd create secret generic repo-charts \
      --from-literal=type=git \
      --from-literal=url="${DFE_REPO_URL}" \
      --from-file=sshPrivateKey="${DFE_REPO_SSH_KEY}" \
      --dry-run=client -o yaml | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | kubectl apply -f -
  elif [[ -n "${DFE_REPO_TOKEN:-}" ]]; then
    echo "  [chart repo] token supplied -> creating Argo repository credential"
    kubectl -n argocd create secret generic repo-charts \
      --from-literal=type=git \
      --from-literal=url="${DFE_REPO_URL}" \
      --from-literal=username="${DFE_REPO_USER:-git}" \
      --from-literal=password="${DFE_REPO_TOKEN}" \
      --dry-run=client -o yaml | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | kubectl apply -f -
  else
    echo "  [chart repo] no DFE_REPO_TOKEN/DFE_REPO_SSH_KEY -- assuming ${DFE_REPO_URL} is PUBLIC"
  fi
fi

echo "==> [7/7] Applying ArgoCD AppProjects + bootstrap ApplicationSet"
run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/appproject-bootstrap.yaml"
# Envoy Gateway operator: anonymous OCI Helm repo cred, then the operator app.
# The config app below needs the Gateway API CRDs the operator installs, so
# wait for them to establish before applying it (its sync retry budget is
# finite -- exhausting it on missing CRDs wedges the app until a manual
# refresh).
kubectl -n argocd create secret generic repo-envoyproxy-oci \
  --from-literal=type=helm \
  --from-literal=name=envoyproxy \
  --from-literal=url=registry-1.docker.io/envoyproxy \
  --from-literal=enableOCI="true" \
  --dry-run=client -o yaml | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | run kubectl apply -f -
run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/envoy-gateway-app.yaml"
if [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  echo "  waiting for the Gateway API CRDs (installed by the operator app)..."
  for _ in $(seq 1 60); do
    kubectl get crd httproutes.gateway.networking.k8s.io >/dev/null 2>&1 && break
    sleep 5
  done
  kubectl wait --for condition=Established --timeout=120s \
    crd/gatewayclasses.gateway.networking.k8s.io \
    crd/gateways.gateway.networking.k8s.io \
    crd/httproutes.gateway.networking.k8s.io || \
    echo "  WARNING: Gateway API CRDs not established -- envoy-gateway-config will need a retry"
fi
# NOTE: envoy-gateway-config and network-policies are generated by the
# layer2-platform ApplicationSet, which reads targetRevision from the cluster
# secret. They were applied directly here too until the appset covered them; a
# second copy baked the revision in at apply time, so a bootstrap from a feature
# branch stranded them once that branch was deleted.
# NOTE: keda-scalers chart retired -- KEDA is now folded into each app chart
# (dfe-common.scaledobject helper), driven by the per-instance overlay.
# cluster-addons ApplicationSet uses goTemplate — no envsubst needed
run kubectl apply -f "${SCRIPT_DIR}/../argocd/bootstrap/argocd-cluster-addons.yaml"

# Prove Argo can actually READ the chart repo before declaring bootstrap done.
# An unreadable repo leaves every Application stuck Unknown, so layer2 never
# generates and the failure surfaces much later as missing ClickHouse and Kafka
# -- symptoms that name everything except the cause. Checked by reachability,
# not by URL scheme, so an SSH URL with a key supplied stays valid.
if [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  echo "==> Verifying ArgoCD can read the chart repo"
  repo_err=""
  for _ in $(seq 1 30); do
    # Force a refresh each pass. A ComparisonError is CACHED on the Application,
    # so an Application that failed before the credential existed keeps reporting
    # the old error and this check would fail a repo it can now read.
    kubectl -n argocd annotate applications --all \
      argocd.argoproj.io/refresh=hard --overwrite >/dev/null 2>&1 || true
    sleep 4
    repo_err=$(kubectl -n argocd get applications -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="ComparisonError")].message}{"\n"}{end}' 2>/dev/null | grep -m1 'failed to list refs' || true)
    [[ -z "${repo_err}" ]] && break
  done
  if [[ -n "${repo_err}" ]]; then
    echo "ERROR: ArgoCD cannot read the chart repo ${DFE_REPO_URL}" >&2
    echo "       ${repo_err}" >&2
    echo "       Set DFE_REPO_TOKEN (HTTPS) or DFE_REPO_SSH_KEY (SSH) if it is private." >&2
    exit 1
  fi
  echo "  [ok] chart repo readable"
fi

# Argo CD repo credential for the bundled in-cluster Forgejo deploy repo, so Argo
# can pull it. Uses the Forgejo admin creds. External git repo creds are handled
# in [4c/7] above; this block is the FALLBACK (bundled) path only.
if [[ "${DFE_BUNDLED_DEPLOY_REPO}" == "true" ]] && [[ "${DFE_DRY_RUN:-false}" != "true" ]]; then
  FORGEJO_PW=$(kubectl -n forgejo get secret dfe-forgejo-admin -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)
  if [[ -n "${FORGEJO_PW}" ]]; then
    kubectl -n argocd create secret generic repo-deploy \
      --from-literal=type=git \
      --from-literal=url="${DFE_CONFIG_REPO_URL}" \
      --from-literal=username="${FORGEJO_ADMIN_USER}" \
      --from-literal=password="${FORGEJO_PW}" \
      --dry-run=client -o yaml | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | kubectl apply -f -
  fi
fi
# NOTE: the deploy-repo app-of-apps is retired. The engine no longer authors Argo
# Application/AppProject manifests -- the dfe-layer2-apps ApplicationSet fans out
# one Application per deploy-repo values file (git-files generator). The deploy
# repo's config_repo_url/revision (on the cluster secret) is consumed there.

echo ""
echo "=========================================="
echo "  Bootstrap complete (Build 1: static)    "
echo "=========================================="
echo "  ArgoCD now syncs Layer 2 (Build 2);     "
echo "  the POST waits for it (DFE_POST).       "
echo "=========================================="

# POWER-ON SELF TEST (POST) -- the authoritative end-of-deploy verification, and a
# deployment PARAMETER rather than a bypass (DFE_POST; see the header). Two gates:
#   readiness   -- waits for Argo to converge Layer 2, then FAILS the deploy if
#                  anything is not genuinely Ready (crashloops, 0/N, unmet
#                  replicas). "Argo Healthy"/"pod Running" alone are not enough.
#   integration -- readiness proves pods are Ready; THIS proves the two DEFAULT
#                  ingest pipelines are actually STREAMING DATA end to end:
#                  (1) infra self-telemetry OTel -> HyperDX -> ClickHouse,
#                  (2) receiver -> [kafka main_land ->] loader -> dfe.main.
#                  "The service is up so it must be working" is the trap this closes.
# Choosing a lighter POST is legitimate (a preview, or a stand-up that runs the
# POST separately) -- but whatever we do not run we say we did NOT verify, so a
# green deploy never overstates what was actually proven.
DFE_POST="${DFE_POST:-full}"
case "${DFE_POST}" in
  full)      POST_READINESS=true;  POST_INTEGRATION=true  ;;
  readiness) POST_READINESS=true;  POST_INTEGRATION=false ;;
  off)       POST_READINESS=false; POST_INTEGRATION=false ;;
  *)
    echo "ERROR: DFE_POST must be one of: full | readiness | off (got '${DFE_POST}')" >&2
    exit 1
    ;;
esac
# Legacy per-gate vars win where explicitly set, so existing callers keep working.
if [ "${DFE_SKIP_READINESS_GATE:-false}" = "true" ]; then POST_READINESS=false; fi
if [ "${DFE_SKIP_INTEGRATION_TESTS:-false}" = "true" ]; then POST_INTEGRATION=false; fi

echo ""
echo "  POST: DFE_POST=${DFE_POST} (readiness=${POST_READINESS}, integration=${POST_INTEGRATION})"

echo ""
if [ "${POST_READINESS}" = "true" ]; then
  # DFE_NS names the namespace the apps land in; without it the gate cannot tell
  # an empty deploy from a healthy one. DFE_ENV decides whether the deployment is
  # allowed to be running the shipped admin password.
  if ! DFE_NS="${DFE_NAMESPACE}" \
       DFE_ENV="${DFE_ENV}" \
       "${SCRIPT_DIR}/smoke-test-readiness.sh" "${KUBECONFIG:-}"; then
    echo ""
    echo "  DEPLOY NOT HEALTHY -- see the readiness failures above."
    echo "  Fix them and re-run, or set DFE_POST=off to stand up without verifying."
    exit 1
  fi
else
  echo "  POST readiness gate NOT RUN -- deploy health NOT verified."
fi

echo ""
if [ "${POST_INTEGRATION}" = "true" ]; then
  # Hand the suite the namespaces + tier THIS deploy actually used. Without them it
  # falls back to its own defaults, which silently point at namespaces the deploy
  # never created (DFE_NAMESPACE is deployer-chosen), and the checks assert nothing.
  if ! DFE_NS="${DFE_NAMESPACE}" \
       DFE_PROFILE="${DFE_PROFILE:-}" \
       "${SCRIPT_DIR}/smoke-test-integration.sh" "${KUBECONFIG:-}"; then
    echo ""
    echo "  DEPLOY PODS HEALTHY but a CORE PIPELINE is NOT flowing -- see failures above."
    echo "  Fix them and re-run, or set DFE_POST=readiness to stand up without the e2e proof."
    exit 1
  fi
else
  echo "  POST integration gate NOT RUN -- data flow NOT verified."
fi

# Post-deploy ACCESS SUMMARY -- endpoints + how to log in + how to fetch creds.
# Reached once every POST gate that was ENABLED has passed -- so under
# DFE_POST=readiness the pipelines are unproven, and under DFE_POST=off nothing
# was verified at all. Printed here AND written to a file.
echo ""
"${SCRIPT_DIR}/access-summary.sh" "${KUBECONFIG:-}" "${DFE_ACCESS_OUT:-dfe-access.md}" || \
  echo "  (access-summary skipped -- run bootstrap/access-summary.sh manually)"

# The launcher's own copy: the two minted passwords in plaintext, 0600, on the
# machine that ran the deploy. The summary above gives fetch commands, which
# need a cluster login the operator does not have yet.
echo ""
python3 "$(cd "${SCRIPT_DIR}/.." && pwd)/scripts/dfe-ops" access-summary \
  --out "${DFE_ACCESS_SUMMARY_OUT:-.tmp/access-summary.md}" \
  --namespace "${DFE_NAMESPACE:-}" || \
  echo "  (login summary skipped -- run scripts/dfe-ops access-summary manually)"
