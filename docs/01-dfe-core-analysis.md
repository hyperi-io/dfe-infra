# DFE Core 2.1 -- Comprehensive Infrastructure Analysis

**Date:** 2026-03-30
**Version Analysed:** 0.8.7 (dfe-core repository)
**Purpose:** Inform the DFE 2.2 upgrade (multi-cloud, OTel/HyperDX, Valkey, PostgreSQL 17, KEDA, OIDC/OAuth2)

---

## 1. Architecture Overview

### 1.1 Overall Structure and Design Philosophy

The dfe-core repository implements a **single-stage Terraform deployment** (`stage_1/`) that provisions all AWS infrastructure, then bootstraps a GitOps pipeline via ArgoCD to deploy Kubernetes workloads. The design philosophy is:

- **Terraform for cloud infrastructure** -- VPC, EKS, MSK, KMS, IAM, Cognito, ClickHouse Cloud, S3, CloudWatch, Lambda
- **ArgoCD for Kubernetes workloads** -- all in-cluster applications are managed as ArgoCD `ApplicationSet` resources using app-of-apps pattern
- **Tenancy-based sizing** -- configurable deployment sizes (dev/small/large) via `.auto.tfvars` profiles
- **Secrets in AWS Secrets Manager** -- synced to Kubernetes via External Secrets Operator (ESO)

**Repository layout:**

```
dfe-core/
  stage_1/              # Single Terraform root module (the deployment entrypoint)
  terraform_modules/    # 9 reusable Terraform modules
  gitOps/               # ArgoCD bootstrap + all GitOps application definitions
  tenancy_sizing/       # dev.auto.tfvars, small.auto.tfvars, large.auto.tfvars
  .github/workflows/    # GitHub Actions CI/CD pipeline (core.yml)
  version.yml           # Semantic version tracking (0.8.7)
```

### 1.2 Component Relationships and Dependencies

The dependency graph (Terraform module order):

```
artifactory_tenant
       |
      misc (KMS keys, S3, Cognito, CloudWatch, Prometheus, Secrets, PCA, SQS)
      / \       \
    vpc   |      |
    / \   |      |
  msk  eks |     |
   |    |  |     |
   +----+--+     |
        |         |
       irsa       |
        |         |
  msk_bootstrap   |
        |         |
   clickhouse ----+
```

**Post-Terraform chain (in CI/CD):**

1. `terraform apply` creates all AWS resources
2. CI renders `argocd_init.yaml` template (namespace, cluster secret, repo secrets)
3. CI installs ArgoCD via Helm
4. CI applies `argocd_bootstrap.yaml` and `cluster_bootstrap.yaml`
5. ArgoCD takes over: syncs `gitOps/bootstrap/` (projects), then `gitOps/addons/argo_apps/` (all ApplicationSets)
6. Separate `Clickhouse_Bootstrap` job runs SQL init scripts via `clickhouse-client`
7. Separate `Deploy_Vector_Core_Pipelines` job triggers the `pipelines` repo

### 1.3 Deployment Topology

Per tenancy, the infrastructure creates:

- **1 VPC** (`10.8.0.0/16` default) with 7 subnet tiers across 3 AZs:
  - EKS subnets (`/21` x 3)
  - ELB internal subnets (`/22` x 3)
  - MSK subnets (`/22` x 3)
  - Management subnets (`/22` x 3)
  - Public subnets (`/22` x 3)
  - VPN subnets (`/22` x 3)
  - 3 NAT Gateways (one per AZ) with EIPs
- **1 EKS cluster** (v1.33, ARM64/Graviton `c7g.2xlarge` default, 3-6 nodes)
  - Addons: VPC-CNI, CoreDNS, kube-proxy, EBS CSI, GuardDuty, S3 CSI
  - Karpenter NodePools for: `vector` (c8gn ARM), `vector-receiver` (r7g ARM), `hunts` (r7g ARM)
- **1 MSK cluster** (3-6 brokers, SASL/IAM + SASL/SCRAM auth, zstd compression, 72h retention)
- **1 ClickHouse Cloud service** (1-3 replicas, BYOC or Cloud mode, idle scaling support)
- **AWS Cognito** user pool with 4 app clients (ArgoCD, Conduktor, Grafana, OAuth2 Proxy)
- **3 KMS keys** (logs, data, EBS) with strict key policies
- **3 S3 buckets** (archive with Glacier tiering, config, PCA CRL)
- **AWS Managed Prometheus** workspace
- **Optional:** Collector VPN and Developer VPN via AWS Client VPN + PCA
- **1 Lambda function** for MSK ACL bootstrap (Python 3.13, runs in VPC)
- **1 Artifactory tenant user** (read-only, for image pulls)

### 1.4 Terraform, Helm, and ArgoCD Orchestration

**Bridge mechanism:** Terraform outputs are injected as annotations on the ArgoCD in-cluster secret (`argocd-in-cluster`) via the template `stage_1/templates/argocd_init.yaml.tpl`. This secret contains annotations with all dynamic values:

```yaml
annotations:
  tenancy_name, account_id, aws_pca_arn, karpenter_sqs_arn,
  prometheus_endpoint, ebs_kms_id, addons_repo, workloads_repo,
  region, vpc_id, public_domain, clickhouse_host, oidc_url, tenancy_size
```

Every ArgoCD `ApplicationSet` uses a **matrix generator** that reads the cluster secret and merges its annotations into Helm `values:` blocks. This is the core pattern:

```yaml
generators:
  - matrix:
      generators:
        - clusters:
            selector:
              matchLabels:
                argocd.argoproj.io/secret-type: cluster
        - list:
            elements:
              - chart: <chart-name>
                ...
```

Values reference cluster annotations like `{{ .metadata.annotations.tenancy_name }}`, `{{ .metadata.annotations.public_domain }}`, etc.

---

## 2. Technical Implementation

### 2.1 Terraform Modules

#### `terraform_aws_vpc/` -- VPC and Networking

- **Files:** `vpc.tf`, `locals.tf`, `data.tf`, `endpoints.tf`, `vpn.tf`, `dev_vpn.tf`, `route53.tf`, `security_groups.tf`, `public_access_sg.tf`, `iam.tf`, `lambda.tf`, `cloudwatch.tf`
- **Creates:** VPC, 7 subnet tiers (3 AZ each), IGW, 3 NAT GWs, route tables, VPC flow logs, private Route53 zone (`<tenancy>.local`), GuardDuty VPC endpoint, security groups (VPN, GuardDuty, public ingress)
- **VPN:** Two optional AWS Client VPN endpoints (collector + developer) using AWS PCA certificates
- **Lambda:** VPN client connection handler that creates Route53 DNS records on VPN connect
- **Key outputs:** `vpc`, `eks_subnets`, `msk_subnets`, `management_subnets`, `route53_zone`, `public_ingress_security_group`, `aws_eip`
- **AWS dependencies:** VPC, EC2, Route53, CloudWatch, Lambda, GuardDuty, Client VPN, ACM PCA

#### `terraform_aws_eks/` -- EKS Cluster

- **Files:** `eks.tf`, `eks-nodes.tf`, `eks-addons.tf`, `variabes.tf` (sic), `locals.tf`, `data.tf`, `iam.tf`, `securitygroups.tf`, `cloudwatch.tf`
- **Creates:** EKS cluster, managed node group with launch template (80GB gp3 encrypted), OIDC provider for IRSA, 6 EKS addons, 3 security groups, IAM roles (cluster, nodes, EBS CSI, VPC CNI, S3 CSI), instance profile
- **EKS Addons:** `vpc-cni`, `kube-proxy`, `coredns`, `aws-ebs-csi-driver`, `aws-guardduty-agent`, `aws-mountpoint-s3-csi-driver`
- **Hardcoded patterns:** Subnet CIDR calculations duplicated from VPC module, `AL2023_ARM_64_STANDARD` AMI type default
- **Key outputs:** `eks_cluster`, `eks_oidc_provider`, `eks_node_security_group`
- **AWS dependencies:** EKS, EC2, IAM, KMS, EBS

#### `terraform_aws_msk/` -- Managed Kafka

- **Files:** `msk.tf`, `msk_sasl_creds.tf`, `variables.tf`, `local.tf`, `securityGroup.tf`, `cloudwatch.tf`, `providers.tf`, `data.tf`
- **Creates:** MSK cluster (provisioned mode), 2 configuration profiles (initial permissive + hardened), 4 SASL/SCRAM users (vector, vector-receiver, conduktor, topic-scaler) with random passwords stored in Secrets Manager, SCRAM secret association, security group (ports 9094/9096/9098/2181/2182)
- **Hardened config highlights:** `zstd` compression, 72h retention, 10MB max message, `min.insync.replicas=2`, `unclean.leader.election=false`, dynamic partition sizing by tenancy (dev=3, small=9, large=48)
- **Key outputs:** `msk` (full cluster object), `msk_security_group`
- **AWS dependencies:** MSK, Secrets Manager, KMS, CloudWatch

#### `terraform_aws_misc/` -- Shared Services (KMS, S3, Cognito, Secrets, Prometheus)

- **Files:** `kms_logs.tf`, `kms_data.tf`, `kms_ebs.tf`, `s3.tf`, `s3BucketPolicy.tf`, `cognito.tf`, `aws_secrets.tf`, `prometheus.tf`, `cloudwatch.tf`, `karpenter_sqs.tf`, `pca.tf`, `parameter_store.tf`, `locals.tf`, `variables.tf`, `outputs.tf`
- **Creates:**
  - 3 KMS keys (logs, data, EBS) with detailed policies per service role
  - 3 S3 buckets (archive, config, PCA CRL) with versioning, encryption, lifecycle (Glacier IR at 30d, Glacier at 120d, expire at 720d)
  - AWS Cognito user pool + domain + 4 app clients (ArgoCD, Conduktor, Grafana, OAuth2 Proxy) + default admin user + `global-admins` group
  - 9+ Secrets Manager secrets (OAuth2 proxy, Conduktor, ArgoCD Cognito, Grafana, default user, docker registry, tenant artifactory, DFE apps DB, DFE apps OAuth)
  - AWS Managed Prometheus workspace with configurable retention
  - Karpenter SQS interruption queue
  - Optional AWS PCA (root CA, 10-year validity, RSA 2048)
  - SSM Parameter Store version tag
  - 5 CloudWatch log groups (Prometheus, EKS dataplane/application/host, EKS cluster)
- **Key outputs:** all KMS keys, `prometheus_workspace`, `karpetner_sqs_queue` (typo preserved), `oauth2_proxy_secret`, `conduktor_secret`, `argocd_cognito_secret`, `aws_pca_arn`
- **AWS dependencies:** KMS, S3, Cognito, Secrets Manager, CloudWatch, Prometheus (AMP), SQS, ACM PCA, SSM

#### `terraform_aws_dfe_irsa/` -- IAM Roles for Service Accounts

- **Files:** `karpenter.tf`, `external_secrets.tf`, `vector.tf`, `vector_receiver.tf`, `external_dns.tf`, `cert_manager.tf`, `alb_controller.tf`, `prometheus.tf`, `prometheus_cloudwatch_exporter.tf`, `fluentbit.tf`, `grafana.tf`, `conduktor.tf`, `iam_ack.tf`, `local.tf`, `variables.tf`, `outputs.tf`
- **Creates 13 IRSA roles** (one per K8s service account): karpenter, external-secrets, vector-ingestion, vector-receiver, external-dns, cert-manager, alb-controller, prometheus, prometheus-cloudwatch-exporter, fluentbit, grafana, conduktor, ack-iam-controller
- **Pattern:** Each follows: policy document + policy + assume role document (OIDC federated) + role + attachment
- **AWS dependencies:** IAM, EKS OIDC provider

#### `terraform_click_house_service/` -- ClickHouse Cloud

- **Files:** `service.tf`, `dfe_users.tf`, `db_init.tf`, `variables.tf`, `locals.tf`, `providers.tf`, `ouputs.tf` (typo preserved)
- **Creates:** ClickHouse Cloud service (AWS region, BYOC or Cloud mode), 17 DFE application users with per-user Secrets Manager entries, SQL init scripts (users + roles + settings profiles + audit tables)
- **DFE users:** `dfe_admin`, `dfe_discovery`, `dfe_ui`, `dfe_loader`, `dfe_grafana_infra`, `dfe_hunts_tier_1/2/3`, `dfe_analyst_tier_1/2/3/4/5/6/7/test`
- **Roles hierarchy:** tiered analyst roles with varying `max_execution_time` and `max_memory_usage`
- **Provider dependency:** ClickHouse Terraform provider (v3.3.3)
- **Key outputs:** `clickhouse_url`, `clickhouse_host`, `clickhouse_native_port`, `clickhouse_https_port`

#### `terraform_artifactory_tenant/` -- JFrog Artifactory User

- **Creates:** One `artifactory_unmanaged_user` per tenancy (read-only)
- **Provider dependency:** JFrog Artifactory provider (~> 12.10.0)

#### `terraform_aws_lambda_msk_bootstrap/` -- Kafka ACL Bootstrap

- **Creates:** Lambda function (Python 3.13) deployed in VPC management subnets, creates Kafka ACLs
- **Trigger:** `aws_lambda_invocation` with `timestamp()` in hash ensures it runs on every apply

#### `terraform_aws_gitlab_runner/` -- GitLab Runner (legacy/unused)

- **Not referenced** from `stage_1/stage_a.tf` -- likely legacy from pre-GitHub migration

### 2.2 Helm Charts Deployed via ArgoCD

All ArgoCD ApplicationSets live in `gitOps/addons/argo_apps/`. Each uses the matrix generator pattern.

| ApplicationSet | Chart Version | Namespace | Sync Wave | Project |
|---|---|---|---|---|
| `argocd` | 8.3.5 | `argo-cd` | 5 | infra |
| `ingress-nginx` | 4.12.1 | `ingress-nginx` | 2 | infra |
| `cert-manager` | v1.16.2 | `cert-manager` | 3 | infra |
| `external-dns` | 1.15.0 | `external-dns` | 3 | infra |
| `external-secrets-operator` | 0.14.3 | `external-secrets` | 3 | infra |
| `karpenter` | 1.2.1 | `karpenter` | 3 | infra |
| `keda` | 2.16.1 | `keda` | 3 | infra |
| `metrics-server` | 3.12.1 | `kube-system` | 3 | infra |
| `prometheus` | 67.9.0 | `prometheus` | 4 | infra |
| `grafana` | 8.11.1 | `grafana` | 5 | infra |
| `grafana-operator` | v5.16.0 | `grafana` | 3 | infra |
| `oauth2-proxy` | 7.12.7 | `oauth2-proxy` | 5 | infra |
| `aws-load-balancer-controller` | 1.10.0 | `kube-system` | 4 | infra |
| `ack-iam-controller` | 1.3.14 | `ack-system` | 3 | infra |
| `vertical-pod-autoscaler` | 10.0.0 | `vpa` | 3 | infra |
| `conduktor` | 1.15.0 | `conduktor` | 5 | infra |
| `reloader` | 1.2.2 | `reloader` | 3 | infra |
| `dfe-ui` | 0.4.0 | `dfe-ui` | 5 | dfe-apps |
| `dfe-discovery` | 0.3.0 | `dfe-discovery` | 5 | dfe-apps |
| `dfe-hunts` | 1.0.0 | per-hunt | 5 | vector |
| `dfe-kafka-topic-partitions-scaler` | 0.1.0 | scaler NS | 5 | vector |

### 2.3 ArgoCD Configuration

**App-of-Apps Pattern:**

1. `argocd_bootstrap.yaml` creates an `Application` named `bootstrap` that watches `gitOps/bootstrap/`
2. `cluster_bootstrap.yaml` creates an `Application` named `cluster-addons` that watches `gitOps/addons/argo_apps/` (all ApplicationSets)
3. `gitOps/bootstrap/` contains 3 `AppProject` definitions: `infra`, `vector`, `dfe-apps`

**Sync Policies:**
- All ApplicationSets use `automated: { prune: true, selfHeal: true }`
- Common syncOptions: `CreateNamespace=true`, `Validate=true`, `PruneLast=true`
- Some use `ServerSideApply=true`
- Some have retry with exponential backoff

**ArgoCD itself is managed by ArgoCD** (self-managing pattern) via the `argocd` ApplicationSet at sync wave 5.

**Dex (OIDC) Integration:** ArgoCD uses Dex with an OIDC connector to AWS Cognito.

### 2.4 GitOps Workflow

```
Developer commits to dfe-core (main) or pipelines repo
    |
    v
GitHub Actions triggers (push/PR/workflow_dispatch)
    |
    v
CI Pipeline:
  1. Validate inputs (account ID, CIDR)
  2. Build Lambda package (Python)
  3. Terraform plan (stage_1 with tenancy tfvars)
  4. If apply: terraform apply -> kubectl apply argocd_init -> helm install argocd -> kubectl apply bootstraps
  5. ClickHouse bootstrap (SQL init)
  6. Trigger pipelines repo for Vector pipeline deployment
    |
    v
ArgoCD takes over:
  - Syncs bootstrap -> creates projects
  - Syncs cluster-addons -> creates all ApplicationSets
  - Each ApplicationSet deploys its Helm chart
  - Self-heal loop maintains desired state
```

### 2.5 Secrets Management

**Flow:** Terraform creates secrets in AWS Secrets Manager -> ExternalSecret Operator syncs them to Kubernetes Secrets -> Pods consume them as env vars or volume mounts.

**ClusterSecretStore:** `gitOps/addons/yaml_addons_stage_2/cluster-secret-store.yaml` defines an ESO `ClusterSecretStore` using `aws` provider.

**Concern:** The ClusterSecretStore has `region: ap-southeast-2` hardcoded.

### 2.6 Networking and Ingress

- **Ingress controller:** `ingress-nginx` (v4.12.1) deployed via ArgoCD
- **DNS:** `external-dns` manages Route53 records
- **TLS:** `cert-manager` with two ClusterIssuers (letsencrypt + private CA)
- **Load balancing:** `aws-load-balancer-controller` for NLB/ALB
- **OAuth2:** `oauth2-proxy` with Cognito OIDC, Redis session store

### 2.7 Monitoring/Observability (Current -- To Be Replaced)

1. **AWS Managed Prometheus (AMP)** -- Prometheus remote-write target
2. **kube-prometheus-stack** (v67.9.0) -- In-cluster Prometheus, AlertManager, ServiceMonitors
3. **Grafana** (v8.11.1) -- Dashboards with Cognito SSO
4. **Grafana Operator** (v5.16.0)
5. **FluentBit** -- DaemonSet shipping logs to CloudWatch
6. **PrometheusRules** -- Comprehensive alerting (pod states, OOM, CPU throttling, PVC, node pressure, Karpenter)
7. **CloudWatch** -- 5 log groups per tenancy
8. **MSK monitoring** -- JMX + node exporter

**What 2.2 replaces:** This entire stack gets replaced by OTel -> ClickHouse via HyperDX. The PrometheusRules alerting definitions are valuable reference material.

### 2.8 Autoscaling Configuration (Current)

1. **EKS managed node group** -- `min_size`/`max_size`/`desired_size` (default 3/6/3)
2. **Karpenter** (v1.2.1) -- 3 NodePools (vector, vector-receiver, hunts) with consolidation
3. **KEDA** (v2.16.1) -- Already deployed but usage is in the `pipelines` repo
4. **VPA** (v10.0.0) -- Vertical Pod Autoscaler deployed
5. **ClickHouse idle scaling** -- Configurable per tenancy

### 2.9 Database Pattern

- **CloudNativePG** (v1.27.0) -- Full CNPG operator CRDs deployed
- **Conduktor PostgreSQL** -- 3-instance CNPG `Cluster` with PG 17.4, encrypted storage, TLSv1.3, pgaudit
- **Storage class:** `ebs-encrypted` using `kubernetes.io/aws-ebs` with gp3, xfs, KMS encryption

---

## 3. Installation Dependencies

### 3.1 AWS-Specific Dependencies (To Be Removed/Abstracted)

| Component | AWS Service | Replacement Strategy |
|---|---|---|
| EKS | `aws_eks_cluster`, `aws_eks_node_group`, `aws_eks_addon` | Cloud-native K8s (EKS/GKE/AKS) or Rancher for local |
| MSK | `aws_msk_cluster`, SASL/SCRAM | Self-hosted Kafka or cloud Kafka |
| IRSA | OIDC-federated IAM roles | Workload identity per cloud or OpenBao |
| ALB Controller | `aws_load_balancer_controller` | Envoy Gateway (replacing nginx + ALB) |
| Route53 | `aws_route53_zone`, DNS01 solver | Cloud DNS or external DNS providers |
| Secrets Manager | `aws_secretsmanager_secret` + ESO | OpenBao/Vault + ESO multi-backend |
| Cognito | `aws_cognito_user_pool` | Envoy Gateway OIDC + external IdP |
| KMS | 3 `aws_kms_key` | Cloud KMS abstraction or OpenBao transit |
| S3 | 3 buckets | MinIO or cloud object storage |
| CloudWatch | Log groups, FluentBit | OTel Collector + HyperDX |
| AMP | `aws_prometheus_workspace` | Replaced by OTel/HyperDX |
| Client VPN | `aws_ec2_client_vpn_endpoint` + PCA | dfe-openvpn or WireGuard |
| Lambda | MSK ACL bootstrap | K8s Job |
| SQS | Karpenter interruption queue | Karpenter native or cloud queue |

### 3.2 External Service Dependencies

| Service | Purpose | Auth Method |
|---|---|---|
| ClickHouse Cloud | Analytics storage | API token |
| JFrog Artifactory | Container + Helm registry | Access token |
| GitHub | Source repos | PAT |
| Let's Encrypt | Public TLS | ACME + DNS01 |

### 3.3 Tool Versions

| Tool | Version |
|---|---|
| Terraform | 1.12 |
| AWS Provider | 6.7.0 |
| ClickHouse Provider | 3.3.3 |
| Kubernetes | 1.33 |
| ArgoCD Helm | 8.3.5 |
| Karpenter | 1.2.1 |

---

## 4. Lessons Learned / Patterns Worth Preserving

### 4.1 What Works Well

1. **Cluster secret annotation bridge** -- Terraform outputs injected as annotations on ArgoCD cluster secret, read by ApplicationSets. Cloud-agnostic at the GitOps layer. **Preserve.**
2. **ApplicationSet matrix generator** -- Clean parameterisation pattern. **Preserve.**
3. **Multi-source ApplicationSets** -- `$values` ref for Helm values from git. **Preserve.**
4. **Sync wave ordering** -- Prevents race conditions (2: ingress, 3: core infra, 4: monitoring, 5: apps). **Preserve.**
5. **Tenancy sizing profiles** -- `.auto.tfvars` files with tagged sizes. **Preserve and extend.**
6. **Comprehensive ClickHouse RBAC** -- Tiered analyst roles with execution/memory limits. **Preserve.**
7. **MSK hardened configuration** -- Production-tuned Kafka broker settings. **Preserve.**
8. **PrometheusRules alerting** -- Comprehensive alerting definitions. **Migrate to OTel/HyperDX.**
9. **KMS key separation** -- Three separate keys (logs, data, EBS). **Preserve concept.**
10. **Reloader integration** -- Pods restart on secret changes. **Preserve.**

### 4.2 What's Fragile or Problematic

1. **Subnet CIDR calculation duplication** -- Same `cidrsubnet()` in 3 modules
2. **Hardcoded values in GitOps** -- `region: ap-southeast-2`, `cluster.name: ghostburner` in YAML files
3. **Terraform backend on JFrog** -- Unusual, creates Artifactory dependency for state
4. **Lambda always re-runs** -- `timestamp()` in hash breaks idempotency
5. **OAuth2 proxy uses Redis** -- Should become Valkey
6. **EKS API endpoint public** -- `endpoint_public_access = true`
7. **Destroy workflow fragility** -- Manual state removal before destroy

### 4.3 Hardcoded Assumptions That Need Abstracting

| Assumption | Impact on Multi-Cloud |
|---|---|
| 3 AZs always available | Not all regions/clouds have 3 AZs |
| ARM64 (Graviton) instances | Different instance families per cloud |
| `kubernetes.io/aws-ebs` provisioner | Must use cloud-specific CSI |
| `eks.amazonaws.com/role-arn` annotation | GCP/Azure use different workload identity |
| `karpenter.k8s.aws/v1` API | Karpenter is AWS-specific |
| AWS Cognito URLs | Must abstract to generic OIDC |
| `AmazonMSK_` secret prefix | MSK-specific |
| `arn:aws:` ARN format | Cloud-specific IAM |

### 4.4 Tenancy/Sizing Patterns

| Parameter | dev | small | large |
|---|---|---|---|
| MSK brokers | 3 | 6 | 6 |
| MSK storage (GB) | 200 | 400 | 2500 |
| ClickHouse replicas | 1 | 3 | 2 |
| ClickHouse memory (GB) | 12 | 48 | 48 |
| EKS nodes | 3-6 (desired 3) | 3-6 (desired 3) | 3-6 (desired 4) |

---

## 5. Migration Considerations

### 5.1 What Can Be Reused Directly

1. **GitOps structure and patterns** -- ArgoCD app-of-apps, ApplicationSets, sync waves
2. **ArgoCD bootstrap projects** -- `infra`, `vector`, `dfe-apps` with RBAC
3. **ClickHouse RBAC SQL** -- Tiered user/role system
4. **Kafka configuration** -- Hardened broker settings (cloud-agnostic)
5. **Helm values structure** -- `common.yaml` + `dev.yaml` per chart
6. **PrometheusRules** -- Translatable to OTel/HyperDX alerting
7. **cert-manager + external-dns** -- Cloud-agnostic with config changes
8. **Reloader, metrics-server, VPA** -- Fully cloud-agnostic
9. **CNPG for PostgreSQL** -- Already using CloudNativePG v1.27.0 with PG 17.4

### 5.2 What Needs Adaptation

1. **VPC/Networking** -- Abstract for AWS VPC, GCP VPC, Azure VNet
2. **EKS** -- Cloud-native K8s per target (EKS/GKE/AKS) or Rancher for local
3. **IRSA** -- Workload identity abstraction per cloud
4. **Secrets management** -- OpenBao as central store, ESO vault provider
5. **KMS** -- Cloud KMS abstraction per provider
6. **Storage classes** -- Cloud-specific CSI drivers
7. **Karpenter** -- AWS-only; needs alternatives for GCP/Azure/local
8. **Cognito** -- Replace with Envoy Gateway OIDC + external IdP

### 5.3 What Should Be Replaced Entirely

1. **Prometheus + Grafana + CloudWatch + FluentBit** -> OTel + ClickHouse + HyperDX
2. **AWS Cognito** -> Envoy Gateway with external OIDC provider
3. **Redis (oauth2-proxy)** -> Valkey
4. **ingress-nginx + ALB Controller** -> Envoy Gateway
5. **MSK** -> Self-hosted Kafka (Strimzi) or cloud Kafka
6. **Lambda functions** -> Kubernetes Jobs
7. **Terraform Cloud on JFrog** -> Cloud-agnostic backend

### 5.4 Risk Areas

1. **IRSA -> Workload Identity** -- Highest-risk change, affects all workloads
2. **Secrets migration** -- Zero-downtime move from AWS SM to OpenBao
3. **MSK -> self-hosted Kafka** -- Complex auth/ACL setup
4. **ClickHouse Cloud -> self-hosted** -- If removing BYOC mode
5. **Cognito -> external OIDC** -- All OAuth2 flows must be updated
6. **Karpenter is AWS-only** -- Fundamentally different autoscaling on other clouds

---

## Appendix: File Index

### stage_1/
- `stage_1/main.tf` -- Provider config (AWS 6.7.0, ClickHouse 3.3.3, Artifactory ~12.10.0)
- `stage_1/stage_a.tf` -- Module invocations (all 8 modules)
- `stage_1/variables.tf` -- 30+ variables with defaults
- `stage_1/outputs.tf` -- 6 outputs
- `stage_1/gitops.tf` -- Template rendering for ArgoCD init
- `stage_1/templates/argocd_init.yaml.tpl` -- K8s namespace + 4 secrets
- `stage_1/templates/argocd/common.yaml` -- ArgoCD Helm values (36KB+)
- `stage_1/src/lambda_function.py` -- MSK ACL bootstrap Lambda

### terraform_modules/
- `terraform_aws_vpc/` -- 12 files
- `terraform_aws_eks/` -- 9 files
- `terraform_aws_msk/` -- 10 files
- `terraform_aws_misc/` -- 14 files
- `terraform_aws_dfe_irsa/` -- 16 files
- `terraform_click_house_service/` -- 9 files
- `terraform_artifactory_tenant/` -- 5 files
- `terraform_aws_lambda_msk_bootstrap/` -- 8 files
- `terraform_aws_gitlab_runner/` -- 11 files (legacy/unused)

### gitOps/
- `gitOps/argocd_bootstrap.yaml` -- Root Application
- `gitOps/cluster_bootstrap.yaml` -- Cluster addons Application
- `gitOps/bootstrap/` -- 3 AppProject definitions
- `gitOps/addons/argo_apps/` -- 24 ApplicationSet YAMLs
- `gitOps/addons/helm/` -- 22 chart value directories
- `gitOps/addons/yaml_infra/` -- CNPG CRDs, FluentBit, EBS StorageClasses
- `gitOps/addons/yaml_addons_stage_1/` -- ClusterIssuers, Karpenter NodePools
- `gitOps/addons/yaml_addons_stage_2/` -- ClusterSecretStore, Conduktor PG, PrometheusRules
- `gitOps/addons/grafana/` -- Dashboards and datasources
