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
# The Debian base is tag@sha256 (toolbox.debian), declared before FROM so FROM can read it.
ARG DEBIAN_TAG="trixie-slim@sha256:a99cfc517144bc59b1978475ec53b46ecabec7e43635402ee5b77cc54cd1b20a"
FROM debian:${DEBIAN_TAG}

ARG TARGETARCH

ARG KUBECTL_VERSION="v1.35.9"
ARG HELM_VERSION="v4.3.0"
ARG ARGOCD_VERSION="v3.5.4"
ARG TOFU_VERSION="1.12.7"
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
       jq \
       dnsutils \
       netcat-openbsd \
       kcat \
       postgresql-client \
       openssl \
    && rm -rf /var/lib/apt/lists/*

# kubectl: official static binary.
RUN curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# helm: official tarball.
RUN curl -fsSL -o /tmp/helm.tar.gz \
      "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" \
    && tar -xzf /tmp/helm.tar.gz -C /tmp \
    && install -m 0755 "/tmp/linux-${TARGETARCH}/helm" /usr/local/bin/helm \
    && rm -rf /tmp/helm.tar.gz "/tmp/linux-${TARGETARCH}"

# argocd CLI: official static binary, matched to the ArgoCD APP version this
# stack's bootstrap.argocd chart deploys (not the chart's own version number).
RUN curl -fsSL -o /usr/local/bin/argocd \
      "https://github.com/argoproj/argo-cd/releases/download/${ARGOCD_VERSION}/argocd-linux-${TARGETARCH}" \
    && chmod +x /usr/local/bin/argocd

# tofu: official release zip. OpenTofu, never the terraform binary.
RUN curl -fsSL -o /tmp/tofu.zip \
      "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_${TARGETARCH}.zip" \
    && unzip -q /tmp/tofu.zip -d /tmp/tofu \
    && install -m 0755 /tmp/tofu/tofu /usr/local/bin/tofu \
    && rm -rf /tmp/tofu.zip /tmp/tofu

# yq: mikefarah/yq, the Go binary -- NOT Debian's python3-yq.
RUN curl -fsSL -o /usr/local/bin/yq \
      "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${TARGETARCH}" \
    && chmod +x /usr/local/bin/yq

# clickhouse-client: the official standalone tgz, pinned to the SAME version
# the stack's ClickHouse server runs (services.clickhouse-version), so a
# compatibility query never drifts from what it actually talks to. Two
# tarballs: clickhouse-common-static carries the real binary,
# clickhouse-client is a thin wrapper + config whose `clickhouse-client`
# entry point is a symlink into it -- common-static installs first. Each
# ships its own install/doinst.sh, which copies relative to the CWD; the
# Dockerfile's default WORKDIR (/) is what makes that land at the real /usr.
RUN curl -fsSL -o /tmp/ch-common.tgz \
      "https://packages.clickhouse.com/tgz/stable/clickhouse-common-static-${CLICKHOUSE_VERSION}-${TARGETARCH}.tgz" \
    && curl -fsSL -o /tmp/ch-client.tgz \
      "https://packages.clickhouse.com/tgz/stable/clickhouse-client-${CLICKHOUSE_VERSION}-${TARGETARCH}.tgz" \
    && mkdir -p /tmp/ch-common /tmp/ch-client \
    && tar -xzf /tmp/ch-common.tgz -C /tmp/ch-common \
    && tar -xzf /tmp/ch-client.tgz -C /tmp/ch-client \
    && "/tmp/ch-common/clickhouse-common-static-${CLICKHOUSE_VERSION}/install/doinst.sh" \
    && "/tmp/ch-client/clickhouse-client-${CLICKHOUSE_VERSION}/install/doinst.sh" \
    && rm -rf /tmp/ch-common.tgz /tmp/ch-client.tgz /tmp/ch-common /tmp/ch-client

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
