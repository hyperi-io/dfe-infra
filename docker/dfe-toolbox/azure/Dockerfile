# dfe-toolbox-azure -- dfe-toolbox-base plus az (azure-cli). See
# docs/deployment/toolbox.md. Separated from the base image because the CLI's
# self-contained venv under /opt/az is close to 1 GB.
#
# ARG default mirrors versions.yaml's toolbox.az-cli pin (drift-checked); the
# workflow overrides it at build time from the same source.
ARG BASE_TAG="v1.0.6"

FROM ghcr.io/hyperi-io/dfe-toolbox-base:${BASE_TAG}

ARG AZ_CLI_VERSION="2.91.0"

USER 0

# az: Microsoft's signed apt repo, key BC528686B50D79E339D3721CEB3E94ADBE1229CF
# pinned by primary fingerprint and trusted for this repo only (signed-by).
# The repo has no `trixie` suite yet (https://packages.microsoft.com/repos/azure-cli/dists/).
# The bookworm build is a self-contained Python venv under /opt/az with no
# glibc assumption trixie does not already meet, so it installs and runs here.
# Re-point this at `trixie` the day Microsoft publishes one.
RUN mkdir -p /etc/apt/keyrings /tmp/keys \
    && curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/keys/microsoft.asc \
      "https://packages.microsoft.com/keys/microsoft.asc" \
    && pinned-keyring /tmp/keys/microsoft.asc /etc/apt/keyrings/microsoft.gpg \
      BC528686B50D79E339D3721CEB3E94ADBE1229CF \
    && echo "deb [arch=amd64,arm64 signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ bookworm main" \
      > /etc/apt/sources.list.d/azure-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "azure-cli=${AZ_CLI_VERSION}-1~bookworm" \
    && rm -rf /var/lib/apt/lists/* /tmp/keys

USER 1000:1000
