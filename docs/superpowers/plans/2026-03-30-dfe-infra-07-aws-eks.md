# DFE Infra 07 — AWS EKS Target

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add AWS EKS as the first cloud deployment target — tf-k8s-cluster (EKS), tf-networking (VPC), tf-iam (IRSA), tf-secrets (AWS SM), tf-dns (Route53) Terraform modules — proving that Layer 2 deploys unchanged on a cloud K8s cluster with the same ApplicationSets.

**Architecture:** Cloud-specific Terraform modules provision EKS + VPC + IAM + secrets backend. The same bootstrap.sh + bridge.py flow runs against the cloud cluster. The same Layer 2 charts (wave 4-5) deploy via ArgoCD with no modifications — only `argocd/values/aws.yaml` overrides differ. This validates the two-layer cloud-agnostic architecture.

**Tech Stack:** Terraform/OpenTofu, AWS provider, EKS, VPC, IAM (IRSA), AWS Secrets Manager, Route53, Helm 3

**Test case:** Point to a blank AWS account → supply a subdomain (e.g. dfe.devex.hyperi.io) → optional OIDC → deploy → it just works.

---

## File Structure

```
terraform/modules/
├── tf-k8s-cluster/
│   ├── variables.tf        # cloud, region, cluster_name, node_groups, k8s_version
│   ├── main.tf             # EKS cluster + managed node groups (aws variant)
│   ├── outputs.tf          # cluster_endpoint, cluster_ca, kubeconfig_command
│   └── eks/
│       ├── main.tf         # aws_eks_cluster, aws_eks_node_group
│       ├── variables.tf
│       └── outputs.tf
│
├── tf-networking/
│   ├── variables.tf        # cloud, vpc_cidr, azs, public/private subnets
│   ├── main.tf             # VPC + subnets + NAT + IGW (aws variant)
│   └── outputs.tf          # vpc_id, subnet_ids, security_group_ids
│
├── tf-dns/
│   ├── variables.tf        # cloud, domain, zone_id
│   ├── main.tf             # Route53 hosted zone + wildcard record (aws variant)
│   └── outputs.tf          # zone_id, nameservers
│
└── tf-secrets/ (existing)  # Add AWS SM variant alongside OpenBao

terraform/environments/aws/
├── main.tf                 # Root module: all AWS modules + bootstrap outputs
├── variables.tf            # AWS-specific: region, account_id, domain
├── outputs.tf              # DFE_* env vars for bootstrap.sh
├── terraform.tfvars.example # Template for customer-specific values
└── backend.tf              # S3 backend with DynamoDB locking
```

---

## Chunk 1: AWS Terraform Modules

### Task 1: tf-networking Module (AWS VPC)

**Files:**
- Create: `terraform/modules/tf-networking/variables.tf`
- Create: `terraform/modules/tf-networking/main.tf`
- Create: `terraform/modules/tf-networking/outputs.tf`

- [ ] **Step 1: Create `variables.tf`**

  ```hcl
  terraform {
    required_providers {
      aws = {
        source  = "hashicorp/aws"
        version = "~> 5.0"
      }
    }
  }

  variable "cloud" {
    description = "Target cloud platform"
    type        = string
  }

  variable "project" {
    description = "Project name for resource tagging"
    type        = string
    default     = "dfe"
  }

  variable "env" {
    description = "Environment name"
    type        = string
  }

  variable "region" {
    description = "AWS region"
    type        = string
    default     = "ap-southeast-2"
  }

  variable "vpc_cidr" {
    description = "VPC CIDR block"
    type        = string
    default     = "10.0.0.0/16"
  }

  variable "availability_zones" {
    description = "List of AZs to use (default: first 3 in region)"
    type        = list(string)
    default     = []
  }

  variable "tags" {
    description = "Additional tags for all resources"
    type        = map(string)
    default     = {}
  }
  ```

- [ ] **Step 2: Create `main.tf`**

  ```hcl
  # terraform/modules/tf-networking/main.tf
  # AWS VPC with public and private subnets.
  # For local cloud: this module is not used (flat network, no VPC).

  data "aws_availability_zones" "available" {
    count = var.cloud == "aws" ? 1 : 0
    state = "available"
  }

  locals {
    azs = var.cloud == "aws" ? (
      length(var.availability_zones) > 0 ? var.availability_zones : slice(data.aws_availability_zones.available[0].names, 0, 3)
    ) : []
    private_subnets = [for i, az in local.azs : cidrsubnet(var.vpc_cidr, 4, i)]
    public_subnets  = [for i, az in local.azs : cidrsubnet(var.vpc_cidr, 4, i + 4)]
  }

  module "naming" {
    source    = "../tf-naming"
    project   = var.project
    component = "vpc"
    env       = var.env
    cloud     = var.cloud
  }

  resource "aws_vpc" "this" {
    count      = var.cloud == "aws" ? 1 : 0
    cidr_block = var.vpc_cidr
    enable_dns_hostnames = true
    enable_dns_support   = true
    tags = merge(module.naming.common_tags, var.tags, { Name = module.naming.canonical_name })
  }

  resource "aws_internet_gateway" "this" {
    count  = var.cloud == "aws" ? 1 : 0
    vpc_id = aws_vpc.this[0].id
    tags   = merge(module.naming.common_tags, { Name = "${module.naming.canonical_name}-igw" })
  }

  resource "aws_subnet" "private" {
    count             = var.cloud == "aws" ? length(local.azs) : 0
    vpc_id            = aws_vpc.this[0].id
    cidr_block        = local.private_subnets[count.index]
    availability_zone = local.azs[count.index]
    tags = merge(module.naming.common_tags, {
      Name                              = "${module.naming.canonical_name}-private-${local.azs[count.index]}"
      "kubernetes.io/role/internal-elb" = "1"
    })
  }

  resource "aws_subnet" "public" {
    count                   = var.cloud == "aws" ? length(local.azs) : 0
    vpc_id                  = aws_vpc.this[0].id
    cidr_block              = local.public_subnets[count.index]
    availability_zone       = local.azs[count.index]
    map_public_ip_on_launch = true
    tags = merge(module.naming.common_tags, {
      Name                     = "${module.naming.canonical_name}-public-${local.azs[count.index]}"
      "kubernetes.io/role/elb" = "1"
    })
  }

  resource "aws_eip" "nat" {
    count  = var.cloud == "aws" ? 1 : 0
    domain = "vpc"
    tags   = merge(module.naming.common_tags, { Name = "${module.naming.canonical_name}-nat" })
  }

  resource "aws_nat_gateway" "this" {
    count         = var.cloud == "aws" ? 1 : 0
    allocation_id = aws_eip.nat[0].id
    subnet_id     = aws_subnet.public[0].id
    tags          = merge(module.naming.common_tags, { Name = "${module.naming.canonical_name}-nat" })
  }

  resource "aws_route_table" "private" {
    count  = var.cloud == "aws" ? 1 : 0
    vpc_id = aws_vpc.this[0].id
    route {
      cidr_block     = "0.0.0.0/0"
      nat_gateway_id = aws_nat_gateway.this[0].id
    }
    tags = merge(module.naming.common_tags, { Name = "${module.naming.canonical_name}-private-rt" })
  }

  resource "aws_route_table" "public" {
    count  = var.cloud == "aws" ? 1 : 0
    vpc_id = aws_vpc.this[0].id
    route {
      cidr_block = "0.0.0.0/0"
      gateway_id = aws_internet_gateway.this[0].id
    }
    tags = merge(module.naming.common_tags, { Name = "${module.naming.canonical_name}-public-rt" })
  }

  resource "aws_route_table_association" "private" {
    count          = var.cloud == "aws" ? length(local.azs) : 0
    subnet_id      = aws_subnet.private[count.index].id
    route_table_id = aws_route_table.private[0].id
  }

  resource "aws_route_table_association" "public" {
    count          = var.cloud == "aws" ? length(local.azs) : 0
    subnet_id      = aws_subnet.public[count.index].id
    route_table_id = aws_route_table.public[0].id
  }
  ```

- [ ] **Step 3: Create `outputs.tf`**

  ```hcl
  output "vpc_id" {
    value = var.cloud == "aws" ? aws_vpc.this[0].id : ""
  }
  output "private_subnet_ids" {
    value = var.cloud == "aws" ? aws_subnet.private[*].id : []
  }
  output "public_subnet_ids" {
    value = var.cloud == "aws" ? aws_subnet.public[*].id : []
  }
  ```

- [ ] **Step 4: Validate**
  ```bash
  cd terraform/modules/tf-networking && terraform init && terraform validate
  ```

- [ ] **Step 5: Commit**
  ```bash
  git add terraform/modules/tf-networking/
  git commit -m "feat: add tf-networking module (AWS VPC, subnets, NAT, IGW)"
  ```

---

### Task 2: tf-k8s-cluster Module (EKS)

**Files:**
- Create: `terraform/modules/tf-k8s-cluster/variables.tf`
- Create: `terraform/modules/tf-k8s-cluster/main.tf`
- Create: `terraform/modules/tf-k8s-cluster/outputs.tf`

- [ ] **Step 1: Create all files**

  **variables.tf:** cloud, project, env, region, k8s_version, vpc_id, subnet_ids, node_instance_type, node_min/max/desired, tags

  **main.tf:** EKS cluster (IAM role, cluster SG, aws_eks_cluster resource) + managed node group. Uses tf-naming for consistent naming. OIDC provider for IRSA.

  **outputs.tf:** cluster_endpoint, cluster_ca_certificate, cluster_name, oidc_provider_arn, oidc_provider_url, kubeconfig_command

- [ ] **Step 2: Validate**
  ```bash
  cd terraform/modules/tf-k8s-cluster && terraform init && terraform validate
  ```

- [ ] **Step 3: Commit**
  ```bash
  git add terraform/modules/tf-k8s-cluster/
  git commit -m "feat: add tf-k8s-cluster module (AWS EKS with managed node groups + OIDC for IRSA)"
  ```

---

### Task 3: tf-iam AWS IRSA Variant + tf-secrets AWS SM Variant

**Files:**
- Modify: `terraform/modules/tf-iam/main.tf` — add AWS IRSA path alongside OpenBao
- Modify: `terraform/modules/tf-secrets/main.tf` — add AWS Secrets Manager path

For tf-iam: when `cloud == "aws"`, create IAM roles with IRSA trust policy instead of OpenBao AppRoles. The `workload_identity_annotations` output becomes `{"eks.amazonaws.com/role-arn": "arn:aws:iam::...:role/dfe-{service}-{env}-irsa"}`.

For tf-secrets: when `cloud == "aws"`, use AWS Secrets Manager instead of OpenBao. Create secrets in SM, output the SM ARN for ESO.

- [ ] **Step 1: Add AWS IRSA to tf-iam**

  Add conditional resources for `var.cloud == "aws"`:
  - `aws_iam_role` per service with IRSA trust policy (OIDC provider)
  - `aws_iam_role_policy` per service (SM read access to service-specific secret path)
  - Update `workload_identity_annotations` output to return `{"eks.amazonaws.com/role-arn": role_arn}` for AWS

  Add new variables: `eks_oidc_provider_arn`, `eks_oidc_provider_url`

- [ ] **Step 2: Add AWS SM to tf-secrets**

  Add conditional resources for `var.cloud == "aws"`:
  - `aws_secretsmanager_secret` for DFE secrets
  - Output ESO connection info (SM region, no AppRole needed)

  Add new variable: `aws_region`

- [ ] **Step 3: Validate both modules**
  ```bash
  cd terraform/modules/tf-iam && terraform init && terraform validate
  cd terraform/modules/tf-secrets && terraform init && terraform validate
  ```

- [ ] **Step 4: Commit**
  ```bash
  git add terraform/modules/tf-iam/ terraform/modules/tf-secrets/
  git commit -m "feat: add AWS IRSA (tf-iam) and AWS Secrets Manager (tf-secrets) cloud variants"
  ```

---

### Task 4: tf-dns Module (Route53)

**Files:**
- Create: `terraform/modules/tf-dns/variables.tf`
- Create: `terraform/modules/tf-dns/main.tf`
- Create: `terraform/modules/tf-dns/outputs.tf`

- [ ] **Step 1: Create module** — Route53 hosted zone + wildcard A record pointing to the Envoy Gateway NLB.

- [ ] **Step 2: Validate + commit**
  ```bash
  git add terraform/modules/tf-dns/
  git commit -m "feat: add tf-dns module (Route53 hosted zone + wildcard record)"
  ```

---

## Chunk 2: AWS Environment + ESO Provider + Validation

### Task 5: AWS Terraform Environment

**Files:**
- Create: `terraform/environments/aws/main.tf`
- Create: `terraform/environments/aws/variables.tf`
- Create: `terraform/environments/aws/outputs.tf`
- Create: `terraform/environments/aws/terraform.tfvars.example`
- Create: `terraform/environments/aws/backend.tf`

Root module that calls tf-naming + tf-networking + tf-k8s-cluster + tf-secrets + tf-iam + tf-dns + tf-storage for a complete AWS deployment. Outputs all DFE_* env vars for bootstrap.sh.

- [ ] **Step 1: Create all files**

  **backend.tf:** S3 backend with DynamoDB locking
  **variables.tf:** aws_region, domain, account_id, node_instance_type, etc.
  **main.tf:** Wire all modules together
  **outputs.tf:** Same DFE_* pattern as local environment
  **terraform.tfvars.example:** Template with placeholder values

- [ ] **Step 2: Validate**
  ```bash
  cd terraform/environments/aws && terraform init && terraform validate
  ```

- [ ] **Step 3: Commit**
  ```bash
  git add terraform/environments/aws/
  git commit -m "feat: add AWS Terraform environment (EKS + VPC + IRSA + SM + Route53)"
  ```

---

### Task 6: ESO ClusterSecretStore AWS Variant

**Files:**
- Create: `bootstrap/templates/eso-cluster-secret-store-aws.yaml.tpl`
- Modify: `bootstrap/bootstrap.sh` — select ESO template based on DFE_CLOUD

The existing ESO template uses OpenBao AppRole. AWS needs an AWS SM provider.

- [ ] **Step 1: Create AWS ESO template**

  ```yaml
  apiVersion: external-secrets.io/v1beta1
  kind: ClusterSecretStore
  metadata:
    name: dfe-secret-store
  spec:
    provider:
      aws:
        service: SecretsManager
        region: "${DFE_REGION}"
        auth:
          jwt:
            serviceAccountRef:
              name: external-secrets
              namespace: external-secrets
  ```

- [ ] **Step 2: Update bootstrap.sh to select template by cloud**

  Replace the hardcoded ESO template apply with:
  ```bash
  ESO_TEMPLATE="eso-cluster-secret-store.yaml.tpl"
  if [[ "${DFE_CLOUD}" == "aws" ]]; then
    ESO_TEMPLATE="eso-cluster-secret-store-aws.yaml.tpl"
  fi
  envsubst < "${TEMPLATES_DIR}/${ESO_TEMPLATE}" | run kubectl apply -f -
  ```

- [ ] **Step 3: Commit**
  ```bash
  git add bootstrap/
  git commit -m "feat: add AWS SM ESO ClusterSecretStore template + cloud-aware bootstrap"
  ```

---

### Task 7: Update aws.yaml Values + Versions

**Files:**
- Modify: `argocd/values/aws.yaml` — fill in AWS-specific Helm values
- Modify: `versions.yaml` — add AWS provider version

- [ ] **Step 1: Update aws.yaml with real values**

  ```yaml
  # AWS EKS overrides
  global:
    cloud: aws

  storageClass: gp3

  envoyGateway:
    service:
      type: LoadBalancer
      annotations:
        service.beta.kubernetes.io/aws-load-balancer-type: "nlb"
        service.beta.kubernetes.io/aws-load-balancer-scheme: "internet-facing"

  configStorage:
    type: s3

  # ESO uses AWS SM (IRSA auth, no vault)
  vault:
    address: ""  # not used on AWS
  ```

- [ ] **Step 2: Add AWS provider to versions.yaml**

  Under `providers:`:
  ```yaml
  hashicorp-aws: "~> 5.0"
  ```

- [ ] **Step 3: Commit**
  ```bash
  git add argocd/values/aws.yaml versions.yaml
  git commit -m "feat: update AWS values and add AWS provider version"
  ```

---

### Task 8: Smoke Test + Validation Script

**Files:**
- Create: `bootstrap/smoke-test-aws.sh`

- [ ] **Step 1: Create AWS-specific smoke test**

  Extends the Layer 1 smoke test with AWS-specific checks: EKS cluster reachable, IRSA roles exist, NLB provisioned, Route53 records resolve.

- [ ] **Step 2: Commit**
  ```bash
  git add bootstrap/smoke-test-aws.sh
  git commit -m "feat: add AWS EKS smoke test"
  ```

---

## Completion Criteria

- [ ] `terraform validate` passes for tf-networking, tf-k8s-cluster, tf-dns
- [ ] `terraform validate` passes for tf-iam (with AWS IRSA variant)
- [ ] `terraform validate` passes for tf-secrets (with AWS SM variant)
- [ ] `terraform validate` passes for `terraform/environments/aws/`
- [ ] `bash -n bootstrap/bootstrap.sh` passes (with cloud-aware ESO template selection)
- [ ] `argocd/values/aws.yaml` has NLB annotations, gp3 storage class, S3 config
- [ ] `versions.yaml` has `hashicorp-aws: "~> 5.0"`
- [ ] All committed and pushed to main

**Validation (on real AWS account):**
- [ ] `terraform apply` in `environments/aws/` provisions EKS + VPC + IAM + SM + Route53
- [ ] `python3 bootstrap/bridge.py --tf-dir terraform/environments/aws` bootstraps the EKS cluster
- [ ] ArgoCD syncs Layer 2 — SAME charts, SAME ApplicationSets, no modifications
- [ ] `dfe.{domain}` resolves and serves dfe-ui via NLB → Envoy Gateway → HTTPRoute

This proves the two-layer cloud-agnostic architecture: Layer 1 is cloud-specific (TF modules), Layer 2 is identical.
