# dfe-toolbox-aws -- dfe-toolbox-base plus the AWS CLI v2 and the Session
# Manager plugin (for `aws ssm start-session`, e.g. into a private-subnet MSK
# broker). See docs/deployment/toolbox.md.
#
# ARG defaults mirror versions.yaml's toolbox.* pins (drift-checked); the
# workflow overrides them at build time from the same source.
ARG BASE_TAG="v1.0.1"

FROM ghcr.io/hyperi-io/dfe-toolbox-base:${BASE_TAG}

ARG TARGETARCH
ARG AWS_CLI_VERSION="2.36.36"
# AWS ships no versioned download for the Session Manager plugin -- only a
# floating "latest" URL per OS/arch -- so this pin is informational: it
# records the version `latest` resolved to when the pin was last verified,
# and the RUN step below asserts the download still reports it. A mismatch
# means AWS moved `latest` again and this pin (plus the drift check that
# reads it) needs bumping to match.
ARG AWS_SSM_PLUGIN_VERSION="1.2.835.0"

USER 0

# aws-cli v2: official installer, versioned URL (AWS names amd64/arm64 as
# x86_64/aarch64). The bundled installer unpacks its own Python runtime under
# /usr/local/aws-cli, independent of the image's own Python (there is none).
RUN case "${TARGETARCH}" in \
      amd64) AWS_ARCH=x86_64 ;; \
      arm64) AWS_ARCH=aarch64 ;; \
      *) echo "unsupported TARGETARCH ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/awscliv2.zip \
      "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}-${AWS_CLI_VERSION}.zip" \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/awscliv2.zip /tmp/aws

# Session Manager plugin: AWS publishes a .deb per OS family, named
# ubuntu_64bit / ubuntu_arm64 -- Debian consumes the same .deb (dpkg format,
# no Ubuntu-specific dependency).
RUN case "${TARGETARCH}" in \
      amd64) SSM_DEB_ARCH=ubuntu_64bit ;; \
      arm64) SSM_DEB_ARCH=ubuntu_arm64 ;; \
      *) echo "unsupported TARGETARCH ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/session-manager-plugin.deb \
      "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/${SSM_DEB_ARCH}/session-manager-plugin.deb" \
    && dpkg -i /tmp/session-manager-plugin.deb \
    && rm -f /tmp/session-manager-plugin.deb \
    && INSTALLED="$(session-manager-plugin --version)" \
    && if [ "${INSTALLED}" != "${AWS_SSM_PLUGIN_VERSION}" ]; then \
         echo "session-manager-plugin: latest is now ${INSTALLED}, versions.yaml toolbox.aws-session-manager-plugin says ${AWS_SSM_PLUGIN_VERSION} -- bump the pin" >&2; \
         exit 1; \
       fi

USER 1000:1000
