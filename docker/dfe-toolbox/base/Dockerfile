# dfe-toolbox-base -- the DFE ops shell. Debian trixie-slim carrying the CLI
# tools an operator needs against a live DFE deployment: kubectl/helm/argocd
# for the cluster and its GitOps controller, tofu for the IaC that provisioned
# it, clickhouse-client/psql for the two SQL-speaking datastores, kcat for
# Kafka/Redpanda, plus curl/jq/yq/openssl/dig/nc for everything else. See
# docs/deployment/toolbox.md for the full contract.
#
# Deliberately thin: the cloud CLIs are NOT here. gcloud and az each add close
# to 1 GB, so they live in the dfe-toolbox-{aws,gcp,azure} images that FROM
# this one -- nothing in THIS image talks to a specific cloud.
#
# No packet-capture tooling: tcpdump is deliberately not installed. The image
# runs as a non-root pod with no CAP_NET_RAW/CAP_NET_ADMIN, so the binary
# would be dead weight next to a capability nobody can use.
#
# No Kafka console-producer/consumer scripts either: Apache's own ship only
# inside a full Kafka distribution plus a JVM, which is not a "small"
# addition -- kcat already covers Kafka AND Redpanda produce/consume.
#
# No HEALTHCHECK: this is an interactive/exec shell, not a service with a
# port to probe.
#
# Every ARG default below equals versions.yaml's toolbox.* pins;
# scripts/check_versions_drift.py holds the two together, and
# .github/workflows/toolbox-build.yml passes the SAME values as
# --build-arg so a stale default here never reaches a pushed image.
#
# Every download is checked against an anchor this repo pins before it
# installs: a signed vendor apt repo with its key pinned by fingerprint, an
# official image pinned by digest, or a signature from a pinned key. Each
# install step names its anchor.
#
# The images are tag@sha256 (toolbox.debian, toolbox.argocd-cli-image,
# toolbox.yq-image), declared before the first FROM so a FROM can read them.
ARG DEBIAN_TAG="trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad"
ARG ARGOCD_IMAGE="v3.5.6@sha256:8e5cc5bb008df8a09dba439462e58c94c55b7735888d3ebde0c25fc05e4e5335"
ARG YQ_IMAGE="4.54.1@sha256:4b3d9475d65571d28cbb19544d3820ec2945e4c8b2f18279394282b8dc3a592e"

FROM quay.io/argoproj/argocd:${ARGOCD_IMAGE} AS argocd
FROM ghcr.io/mikefarah/yq:${YQ_IMAGE} AS yq

FROM debian:${DEBIAN_TAG}

ARG TARGETARCH

ARG KUBECTL_VERSION="v1.35.9"
ARG HELM_VERSION="v4.3.1"
ARG ARGOCD_VERSION="v3.5.6"
ARG TOFU_VERSION="1.13.2"
ARG YQ_VERSION="v4.54.1"
ARG CLICKHOUSE_VERSION="26.3.42.3"

# Debian packages: everything with no upstream release cadence worth tracking
# as its own versions.yaml pin.
#   - kcat: Debian's `kafkacat` source package (upstream renamed the binary to
#     kcat; the project has had no release since 1.7.1 in 2021, so pinning
#     past what Debian ships buys nothing real).
#   - postgresql-client: pulls postgresql-client-17, matching
#     services.postgresql (CNPG runs PG 17).
# Deliberately unpinned: trixie is a point-release distro and these packages
# ride its own security-update cadence, which a hardcoded version would fight
# on every rebuild.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       curl \
       unzip \
       gnupg \
       gpgv \
       jq \
       dnsutils \
       netcat-openbsd \
       kcat \
       postgresql-client \
       openssl \
    && rm -rf /var/lib/apt/lists/*

# Writes a keyring of only the pinned keys, for this image and the cloud layers.
COPY pinned-keyring.sh /usr/local/sbin/pinned-keyring
RUN chmod 0755 /usr/local/sbin/pinned-keyring

# Signed vendor apt repos, each key pinned by primary fingerprint:
#   kubectl            pkgs.k8s.io              DE15B14486CD377B9E876E1A234654DA9A296436
#   tofu               packages.opentofu.org    F4AF70F66EAC4337EEECC97407D3DFCD4C61499F
#   clickhouse-client  packages.clickhouse.com  3A9EA1193A97B548BE1457D48919F6BD2B48D754
# OpenTofu, never the terraform binary. clickhouse-client is pinned to the
# version the stack's ClickHouse server runs (services.clickhouse-version), so
# a compatibility query never drifts from what it talks to. The repos are
# removed after the install, so the cloud layers' apt-get update reads Debian only.
RUN mkdir -p /etc/apt/keyrings /tmp/keys \
    && curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/keys/kubernetes.asc \
      "https://pkgs.k8s.io/core:/stable:/${KUBECTL_VERSION%.*}/deb/Release.key" \
    && curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/keys/opentofu.asc \
      "https://packages.opentofu.org/opentofu/tofu/gpgkey" \
    && curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/keys/clickhouse.asc \
      "https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key" \
    && pinned-keyring /tmp/keys/kubernetes.asc /etc/apt/keyrings/kubernetes.gpg \
      DE15B14486CD377B9E876E1A234654DA9A296436 \
    && pinned-keyring /tmp/keys/opentofu.asc /etc/apt/keyrings/opentofu.gpg \
      F4AF70F66EAC4337EEECC97407D3DFCD4C61499F \
    && pinned-keyring /tmp/keys/clickhouse.asc /etc/apt/keyrings/clickhouse.gpg \
      3A9EA1193A97B548BE1457D48919F6BD2B48D754 \
    && echo "deb [signed-by=/etc/apt/keyrings/kubernetes.gpg] https://pkgs.k8s.io/core:/stable:/${KUBECTL_VERSION%.*}/deb/ /" \
      > /etc/apt/sources.list.d/kubernetes.list \
    && echo "deb [signed-by=/etc/apt/keyrings/opentofu.gpg] https://packages.opentofu.org/opentofu/tofu/any/ any main" \
      > /etc/apt/sources.list.d/opentofu.list \
    && echo "deb [signed-by=/etc/apt/keyrings/clickhouse.gpg] https://packages.clickhouse.com/deb stable main" \
      > /etc/apt/sources.list.d/clickhouse.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      "kubectl=${KUBECTL_VERSION#v}-*" \
      "tofu=${TOFU_VERSION}" \
      "clickhouse-common-static=${CLICKHOUSE_VERSION}" \
      "clickhouse-client=${CLICKHOUSE_VERSION}" \
    && rm -rf /var/lib/apt/lists/* /tmp/keys \
      /etc/apt/sources.list.d/kubernetes.list /etc/apt/keyrings/kubernetes.gpg \
      /etc/apt/sources.list.d/opentofu.list /etc/apt/keyrings/opentofu.gpg \
      /etc/apt/sources.list.d/clickhouse.list /etc/apt/keyrings/clickhouse.gpg

# helm: release tarball signed by the helm release key
# 208DD36ED5BB3745A16743A4C7C6FBB5B91C1155, read from KEYS at the release tag.
RUN mkdir -p /tmp/helm \
    && curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/helm/KEYS \
      "https://raw.githubusercontent.com/helm/helm/${HELM_VERSION}/KEYS" \
    && pinned-keyring /tmp/helm/KEYS /tmp/helm/helm.gpg \
      208DD36ED5BB3745A16743A4C7C6FBB5B91C1155 \
    && curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/helm/helm.tar.gz \
      "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" \
    && curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/helm/helm.tar.gz.asc \
      "https://github.com/helm/helm/releases/download/${HELM_VERSION}/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz.asc" \
    && gpgv --keyring /tmp/helm/helm.gpg /tmp/helm/helm.tar.gz.asc /tmp/helm/helm.tar.gz \
    && tar -xzf /tmp/helm/helm.tar.gz -C /tmp/helm \
    && install -m 0755 "/tmp/helm/linux-${TARGETARCH}/helm" /usr/local/bin/helm \
    && rm -rf /tmp/helm

# argocd and yq (mikefarah/yq, the Go binary, NOT Debian's python3-yq): copied
# out of their official images by index digest, which is the check.
COPY --from=argocd /usr/local/bin/argocd /usr/local/bin/argocd
COPY --from=yq /usr/bin/yq /usr/local/bin/yq

# The image digests and the release pins are separate versions.yaml keys, so a
# pair naming different releases fails here. argocd tracks the Argo CD APP
# version bootstrap.argocd deploys, not the chart's own version number.
RUN ARGOCD_REPORTED="$(argocd version --client --short)" \
    && case "${ARGOCD_REPORTED}" in \
         "argocd: ${ARGOCD_VERSION}" | "argocd: ${ARGOCD_VERSION}+"*) ;; \
         *) echo "argocd reports '${ARGOCD_REPORTED}', toolbox.argocd-cli says ${ARGOCD_VERSION}" >&2; exit 1 ;; \
       esac \
    && YQ_REPORTED="$(yq --version)" \
    && case "${YQ_REPORTED}" in \
         *" version ${YQ_VERSION}") ;; \
         *) echo "yq reports '${YQ_REPORTED}', toolbox.yq says ${YQ_VERSION}" >&2; exit 1 ;; \
       esac

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Non-root, read-only-rootfs friendly: everything above installs to /usr or
# /usr/local as root; nothing at runtime writes outside $HOME or /tmp. UID
# 1000 matches the DFE convention used elsewhere in this stack (e.g. the
# hunt-runner git-sync sidecar). A pod running this with
# readOnlyRootFilesystem: true must mount both $HOME and /tmp writable
# (emptyDir is enough) -- see docs/deployment/toolbox.md.
RUN groupadd --gid 1000 toolbox \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash toolbox
ENV HOME=/home/toolbox
WORKDIR /home/toolbox
USER 1000:1000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD []
