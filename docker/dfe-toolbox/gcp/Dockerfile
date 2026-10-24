# dfe-toolbox-gcp -- dfe-toolbox-base plus gcloud (google-cloud-cli). See
# docs/deployment/toolbox.md. Separated from the base image because the SDK
# alone is close to 1 GB unpacked.
#
# ARG default mirrors versions.yaml's toolbox.gcloud pin (drift-checked); the
# workflow overrides it at build time from the same source.
ARG BASE_TAG="v1.0.6"

FROM ghcr.io/hyperi-io/dfe-toolbox-base:${BASE_TAG}

ARG GCLOUD_VERSION="587.0.0"

USER 0

# google-cloud-cli: Google's signed apt repo, key
# 35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3 pinned by primary fingerprint. The
# package pulls Debian's python3, which the base image does not carry. The
# installation properties file turns usage reporting off for every user.
RUN mkdir -p /etc/apt/keyrings /tmp/keys \
    && curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/keys/google-cloud.asc \
      "https://packages.cloud.google.com/apt/doc/apt-key.gpg" \
    && pinned-keyring /tmp/keys/google-cloud.asc /etc/apt/keyrings/google-cloud.gpg \
      35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3 \
    && echo "deb [signed-by=/etc/apt/keyrings/google-cloud.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
      > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "google-cloud-cli=${GCLOUD_VERSION}-*" \
    && printf '[core]\ndisable_usage_reporting = true\n' > /usr/lib/google-cloud-sdk/properties \
    && rm -rf /var/lib/apt/lists/* /tmp/keys

USER 1000:1000
