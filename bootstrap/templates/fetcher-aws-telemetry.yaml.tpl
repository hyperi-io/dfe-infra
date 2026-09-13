# Fetcher pre-config for the AWS telemetry the otel sink routes around
# CloudWatch. Rendered by bootstrap.sh only when DFE_CLOUD=aws and
# DFE_TELEMETRY_SINK=otel -- under the cloudwatch sink these touchpoints
# already have an AWS-native destination and there is nothing here for the
# fetcher to read instead.
#
# THIS IS A STARTER FRAGMENT, not a wired dfe-fetcher instance. A real fetcher
# deployment is engine-authored -- one Argo Application per source, built from
# apps.yaml routing into helm/charts/dfe-fetcher's config.sources -- and this
# repo does not run dfe-engine. The ConfigMap below carries the sources:
# stanza an operator or dfe-engine folds into that instance's config.sources,
# in dfe-fetcher's own config schema (dfe-fetcher/docs/cloud-setup/aws.md,
# .../object_store.md).
apiVersion: v1
kind: ConfigMap
metadata:
  name: dfe-fetcher-aws-telemetry-preconfig
  namespace: "${DFE_NAMESPACE}"
  labels:
    dfe.hyperi.io/component: fetcher-preconfig
data:
  sources-fragment.yaml: |
    # Fold this under a dfe-fetcher instance's config.sources. Both
    # access_key_id/secret_access_key fields use the env: indirection
    # (dfe-fetcher/docs/cloud-setup/aws.md, .../object_store.md) -- point them
    # at a real credential before this reads anything; a blank credential just
    # health-checks unhealthy.
    #
    # THIS IS THE ONE CREDENTIAL IN THE STACK THAT CANNOT BE POD IDENTITY.
    # Every other AWS credential here -- Karpenter, the Load Balancer
    # Controller, the EBS CSI driver, external-dns, cert-manager, the MSK
    # bootstrap Job -- runs as a Pod Identity association with no stored
    # secret. dfe-fetcher's AWS source cannot join them: its
    # `resolve_credentials()` (dfe-fetcher src/source/aws/mod.rs) reads
    # `aws.access_key_id`/`aws.secret_access_key` (or a `credential_secret`
    # resolving to the same pair) directly and never calls into the AWS SDK's
    # own credential chain, so there is no code path here for the Pod Identity
    # agent to reach. Track dfe-fetcher for that support; until it lands, mint
    # the narrowest read-only IAM user this source needs (cloudtrail
    # LookupEvents + the one CloudWatch Logs group named below, nothing else --
    # see dfe-fetcher/docs/cloud-setup/aws.md for the policy), put its key pair
    # in this deployment's own secret store (the same aws-sm/OpenBao backend
    # `secrets.backend` already names), and let ESO materialise it into the
    # Secret these env vars read -- never a key exported by hand into the
    # engine-authored Application that runs this config.
    sources:
      # CloudTrail's LookupEvents API returns management events directly --
      # the same scope the trail itself is configured for
      # (terraform/environments/aws/cloudtrail.tf) -- so this needs no bucket
      # name at all, unlike the object_store source below. LookupEvents also
      # windows at 90 days; DFE_CLOUDTRAIL_BUCKET (bootstrap fact, from the
      # root's own output) is the durable alternative if a longer history is
      # ever needed, via a second object_store prefix.
      #
      # The EKS control-plane audit log has no export path but CloudWatch
      # (kubernetes-cluster/CONTRACT.md) -- there IS a documented CloudWatch
      # Logs service under this same aws source (cloudwatch_logs), so it rides
      # alongside cloudtrail rather than needing a gap noted here.
      aws:
        enabled: true
        region: "${DFE_REGION}"
        access_key_id: "env:AWS_ACCESS_KEY_ID"
        secret_access_key: "env:AWS_SECRET_ACCESS_KEY"
        services:
          - name: cloudtrail
          - name: cloudwatch_logs
            config:
              log_group_name: "${DFE_EKS_AUDIT_LOG_GROUP}"
        topic: "aws"
      # MSK's broker logs land in S3 as broker log text, not behind a service
      # API -- object_store is dfe-fetcher's only path to them. AWS's own MSK
      # logging docs name no key layout or format for the S3 destination, so
      # prefix is left open (scans the whole bucket) and format is the
      # conservative text_gz; confirm both against a real delivery before this
      # is more than a starter.
      object_store:
        enabled: true
        topic: "object_store"
        backends:
          - provider: s3
            region: "${DFE_REGION}"
            access_key_id: "env:AWS_ACCESS_KEY_ID"
            secret_access_key: "env:AWS_SECRET_ACCESS_KEY"
            buckets:
              - bucket: "${DFE_KAFKA_BROKER_LOG_BUCKET}"
                prefixes:
                  - prefix: ""
                    format: text_gz
                    source_tag: "msk_broker_logs"
