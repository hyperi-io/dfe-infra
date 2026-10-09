# dfe-toolbox-gcp -- dfe-toolbox-base plus gcloud (google-cloud-cli). See
# docs/deployment/toolbox.md. Separated from the base image because the SDK
# alone is close to 1 GB unpacked.
#
# ARG default mirrors versions.yaml's toolbox.gcloud pin (drift-checked); the
# workflow overrides it at build time from the same source.
ARG BASE_TAG="v1.0.3"

FROM ghcr.io/hyperi-io/dfe-toolbox-base:${BASE_TAG}

ARG TARGETARCH
ARG GCLOUD_VERSION="587.0.0"

USER 0

# gcloud needs a system Python 3 interpreter; the base image ships none
# (nothing else in it is Python-based). Deliberately unpinned: it rides
# trixie's own security-update cadence.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

# google-cloud-cli: official tarball install (Google names amd64/arm64 as
# x86_64/arm). --usage-reporting=false and --command-completion=false keep
# the non-interactive install from prompting; --path-update=false because
# PATH is set explicitly below instead of writing to a shell rc file nothing
# in a container sources.
RUN case "${TARGETARCH}" in \
      amd64) GCLOUD_ARCH=x86_64 ;; \
      arm64) GCLOUD_ARCH=arm ;; \
      *) echo "unsupported TARGETARCH ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/gcloud.tar.gz \
      "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-${GCLOUD_VERSION}-linux-${GCLOUD_ARCH}.tar.gz" \
    && tar -xzf /tmp/gcloud.tar.gz -C /opt \
    && rm -f /tmp/gcloud.tar.gz \
    && /opt/google-cloud-sdk/install.sh \
         --usage-reporting=false \
         --command-completion=false \
         --path-update=false \
         --quiet

ENV PATH="/opt/google-cloud-sdk/bin:${PATH}"

USER 1000:1000
