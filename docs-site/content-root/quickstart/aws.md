---
sidebar_position: 4
sidebar_label: AWS
---

# Quickstart: AWS

OpenTofu builds Amazon EKS on Graviton, with Kafka from Amazon MSK, Confluent Cloud or Redpanda Cloud. Then the same bootstrap as every other cluster deploys DFE onto it.

You need AWS credentials for the account, OpenTofu, the `aws` CLI, `kubectl`, `helm`, `openssl` and Python 3. Apply `terraform/environments/aws-state` once per account first, for the state bucket. `<version>` is the DFE release you are installing, the one in the version menu at the top of this page.

```bash
git clone --branch <version> https://github.com/hyperi-io/dfe-infra && cd dfe-infra
python3 scripts/dfe-ops init
python3 scripts/resolve_sizing.py --dial deployment.yaml --live \
  --out terraform/environments/aws
python3 scripts/render_dial.py --tofu
```

`dfe-ops init` asks nine questions and writes `deployment.yaml`, the one file the deployment reads. Answer `aws` for the target, `scale` for the tier, and `msk`, `confluent-cloud` or `redpanda-cloud` for Kafka. The resolver asks the AWS API what the region offers and sizes the node pools from your GB/day estimate. Leave the estimate blank for the smallest shape that runs.

Confluent Cloud and Redpanda Cloud take their API credentials from your shell, never a file. Export `CONFLUENT_CLOUD_API_KEY` and `CONFLUENT_CLOUD_API_SECRET` for Confluent Cloud before the plan. The Redpanda provider checks for `REDPANDA_CLIENT_ID` and `REDPANDA_CLIENT_SECRET` on every plan, whichever Kafka you chose.

Then build the cluster and deploy onto it:

```bash
(cd terraform/environments/aws && tofu init \
  && tofu plan -out=deployment.tfplan && tofu apply deployment.tfplan)
eval "$(tofu -chdir=terraform/environments/aws output -raw kubeconfig_command)"
python3 scripts/dfe-ops stack-deploy --stack <version> --mode scale \
  --from-terraform terraform/environments/aws
```

The apply builds the VPC, EKS and its node groups, the managed Kafka, the KMS key, the Route 53 zones and the Secrets Manager store. `stack-deploy` reads its outputs, deploys the stack at `<version>`, and finishes by sending an event through to ClickHouse and checking the stack's own telemetry landed.

Next:

1. Every sizing dial, lock and choice: [DFE on AWS](../dfe-infra/deployment/aws.md).
2. Choose the Kafka service: [Kafka](../dfe-infra/deployment/kafka/README.md).
3. Run it day to day: [AWS operations](../dfe-infra/deployment/aws-operations.md).
4. Upgrade, and roll back: [upgrade and rollback](../dfe-infra/deployment/upgrade-rollback.md).
5. Take it down: [AWS teardown](../dfe-infra/deployment/aws-teardown.md).
