# dfe-toolbox-aws -- dfe-toolbox-base plus the AWS CLI v2 and the Session
# Manager plugin (for `aws ssm start-session`, e.g. into a private-subnet MSK
# broker). See docs/deployment/toolbox.md.
#
# ARG defaults mirror versions.yaml's toolbox.* pins (drift-checked); the
# workflow overrides them at build time from the same source.
ARG BASE_TAG="v1.0.6"

FROM ghcr.io/hyperi-io/dfe-toolbox-base:${BASE_TAG}

ARG TARGETARCH
ARG AWS_CLI_VERSION="2.38.5"
ARG AWS_SSM_PLUGIN_VERSION="1.2.896.0"

USER 0

# AWS publishes both signing keys only in its install docs, so they live here.
COPY aws-cli.asc session-manager-plugin.asc /tmp/keys/

# aws-cli v2: zip signed by the AWS CLI Team key
# FB5DB77FD5C118B80511ADA8A6310ACC4672475C (aws-cli.asc). AWS names amd64/arm64
# as x86_64/aarch64. The installer unpacks its own Python runtime under
# /usr/local/aws-cli, independent of the image's own Python (there is none).
RUN case "${TARGETARCH}" in \
      amd64) AWS_ARCH=x86_64 ;; \
      arm64) AWS_ARCH=aarch64 ;; \
      *) echo "unsupported TARGETARCH ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && pinned-keyring /tmp/keys/aws-cli.asc /tmp/keys/aws-cli.gpg \
      FB5DB77FD5C118B80511ADA8A6310ACC4672475C \
    && curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/awscliv2.zip \
      "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}-${AWS_CLI_VERSION}.zip" \
    && curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/awscliv2.zip.sig \
      "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}-${AWS_CLI_VERSION}.zip.sig" \
    && gpgv --keyring /tmp/keys/aws-cli.gpg /tmp/awscliv2.zip.sig /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/awscliv2.zip /tmp/awscliv2.zip.sig /tmp/aws

# Session Manager plugin: this version's .deb, signed by the AWS SSM key
# 7959637124CE093AD501D47A2C4D4AFF6F6757EE (session-manager-plugin.asc). AWS
# names its .deb ubuntu_64bit / ubuntu_arm64, and Debian installs the same file.
RUN case "${TARGETARCH}" in \
      amd64) SSM_DEB_ARCH=ubuntu_64bit ;; \
      arm64) SSM_DEB_ARCH=ubuntu_arm64 ;; \
      *) echo "unsupported TARGETARCH ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && pinned-keyring /tmp/keys/session-manager-plugin.asc /tmp/keys/session-manager-plugin.gpg \
      7959637124CE093AD501D47A2C4D4AFF6F6757EE \
    && curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/session-manager-plugin.deb \
      "https://s3.amazonaws.com/session-manager-downloads/plugin/${AWS_SSM_PLUGIN_VERSION}/${SSM_DEB_ARCH}/session-manager-plugin.deb" \
    && curl -fsSL --proto '=https' --tlsv1.2 -o /tmp/session-manager-plugin.deb.sig \
      "https://s3.amazonaws.com/session-manager-downloads/plugin/${AWS_SSM_PLUGIN_VERSION}/${SSM_DEB_ARCH}/session-manager-plugin.deb.sig" \
    && gpgv --keyring /tmp/keys/session-manager-plugin.gpg /tmp/session-manager-plugin.deb.sig /tmp/session-manager-plugin.deb \
    && dpkg -i /tmp/session-manager-plugin.deb \
    && rm -rf /tmp/session-manager-plugin.deb /tmp/session-manager-plugin.deb.sig /tmp/keys \
    && INSTALLED="$(session-manager-plugin --version)" \
    && if [ "${INSTALLED}" != "${AWS_SSM_PLUGIN_VERSION}" ]; then \
         echo "session-manager-plugin: the ${AWS_SSM_PLUGIN_VERSION} download reports ${INSTALLED}" >&2; \
         exit 1; \
       fi

USER 1000:1000
