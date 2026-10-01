# dfe-toolbox-azure -- dfe-toolbox-base plus az (azure-cli). See
# docs/deployment/toolbox.md. Separated from the base image because the CLI's
# self-contained venv under /opt/az is close to 1 GB.
#
# ARG default mirrors versions.yaml's toolbox.az-cli pin (drift-checked); the
# workflow overrides it at build time from the same source.
ARG BASE_TAG="v1.0.1"

FROM ghcr.io/hyperi-io/dfe-toolbox-base:${BASE_TAG}

ARG AZ_CLI_VERSION="2.90.0"

USER 0

# Microsoft's azure-cli apt repo has no `trixie` suite yet (only as far as
# bookworm at the time this was written -- checked against
# https://packages.microsoft.com/repos/azure-cli/dists/). The bookworm build
# is a self-contained Python venv under /opt/az with no distro-version-specific
# glibc assumptions beyond what trixie already exceeds, so it installs and
# runs cleanly here; re-point this at `trixie` the day Microsoft publishes one.
RUN curl -fsSL "https://packages.microsoft.com/keys/microsoft.asc" -o /tmp/microsoft.asc \
    && gpg --dearmor -o /etc/apt/trusted.gpg.d/microsoft.gpg /tmp/microsoft.asc \
    && rm -f /tmp/microsoft.asc \
    && echo "deb [arch=amd64,arm64] https://packages.microsoft.com/repos/azure-cli/ bookworm main" \
      > /etc/apt/sources.list.d/azure-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "azure-cli=${AZ_CLI_VERSION}-1~bookworm" \
    && rm -rf /var/lib/apt/lists/*

USER 1000:1000
