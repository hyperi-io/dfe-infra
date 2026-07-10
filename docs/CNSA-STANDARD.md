<!--
Project:   DFE (Data Forensics Engine) - product suite
File:      docs/CNSA-STANDARD.md
Purpose:   DFE cryptographic standard at the deployment layer - transport,
           certificate and at-rest crypto. CNSA-aligned classical baseline
           with a hybrid post-quantum transport layer and a migration path
           to full CNSA 2.0.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# DFE cryptographic standard

DFE targets the NSA Commercial National Security Algorithm (CNSA) family,
because national-security deployment is in scope, while remaining deployable in
ordinary commercial environments. The user base is small and high-value, so
algorithm strength is chosen for compliance rather than throughput.

**Precise naming matters here.** DFE's symmetric and hash primitives are at
**CNSA 2.0**; its asymmetric primitives (key establishment and signatures) are
the **CNSA 1.0** classical suite (the Suite B successor), which is what CNSA 2.0
is migrating away from. So the accurate description is: **a CNSA 1.0-aligned
classical baseline, with AES-256/SHA-384 already meeting CNSA 2.0, a hybrid
post-quantum key-exchange layer enabled on the wire today, and a documented
migration to full CNSA 2.0 (ML-KEM-1024, ML-DSA-87)**. We deliberately do not
label a P-384/ES384 baseline "CNSA 2.0" - that suite's whole purpose is
post-quantum, and P-384/ES384 are not on its algorithm list.

This document covers the deployment layer: **transport (TLS), certificates, and
data at rest**. Application signing (the ES384 JWT) is covered by the engine's
auth topology document (`AUTH-ENVOY-TOPOLOGY.md`).

## What "CNSA" means here - the honest map

| Function | DFE today | CNSA 1.0 | CNSA 2.0 | Status |
|---|---|---|---|---|
| Symmetric | AES-256-GCM | AES-256 | AES-256 | **CNSA 2.0** |
| Hash | SHA-384 | SHA-384 | SHA-384 / 512 | **CNSA 2.0** |
| Key establishment | ECDH P-384 **+ hybrid ML-KEM on the wire** | ECDH P-384 | ML-KEM-1024 | CNSA 1.0 classical; hybrid PQC in transit |
| Signatures (certs, JWT) | ECDSA / ES384 P-384 | ECDSA P-384 | ML-DSA-87 | CNSA 1.0 classical |
| Firmware signing | n/a | - | LMS / XMSS | not in scope |

The residual quantum exposure is therefore precisely the asymmetric layer:
**harvest-now-decrypt-later** against the key-exchange (mitigated today by the
hybrid ML-KEM transport layer below) and **forge-later** against long-lived
signing keys and roots (mitigated by short lifetimes + crypto-agility until
ML-DSA is deployable).

## Profiles

DFE ships two profiles; a deployment selects one and every component inherits
its floor.

| Layer | `prod` (default, commercial-safe) | `highsec` (national-security) |
|---|---|---|
| TLS floor | 1.2 minimum, 1.3 preferred | 1.3 only |
| Symmetric | AES-256-GCM | AES-256-GCM |
| Hashing | SHA-384 | SHA-384 / SHA-512 |
| Certificates / signatures | ECDSA P-384, SHA-384 | ECDSA P-384 -> ML-DSA-87 |
| Key establishment | ECDHE P-384 + hybrid ML-KEM where offered | hybrid ML-KEM required, ML-KEM-1024 target |
| SSH | Ed25519, ML-KEM hybrid KEX | Ed25519, ML-KEM hybrid KEX |

`prod` is the corporate default. Its **TLS 1.2 floor** is deliberate: TLS 1.3 is
universal in modern browsers, but 1.3-*only* breaks a long tail of enterprise
middleboxes and legacy API clients, so `prod` requires 1.2 as a floor (removing
SSLv3/TLS 1.0/1.1) with 1.3 preferred, and holds AES-256-GCM at the 1.2 layer
via an explicit cipher list. `highsec` raises the floor to TLS 1.3-only. Both
offer hybrid post-quantum key exchange where the peer supports it.

The design is **crypto-agile**: protocol floors, curves, cipher policy and
signing algorithms are configuration, not code, so raising a profile or adopting
a PQC primitive is a config change.

## Transport (TLS)

- **Edge.** The single Envoy front layer terminates TLS (1.2 floor in `prod`,
  1.3-only in `highsec`), offers `TLS_AES_256_GCM_SHA384` (with
  `ECDHE-ECDSA-AES256-GCM-SHA384` at the 1.2 layer), and negotiates ECDHE on
  P-384. It also offers the hybrid **X25519MLKEM768** group (a TLS 1.3 group;
  see the migration section) so a post-quantum-capable client gets PQC key
  agreement, with classical clients falling back to P-384 - no protocol break.
- **Backing services.** ClickHouse, Kafka, the metadata store and the telemetry
  pipeline speak TLS with certificates issued by the internal certificate
  authority. Broker authentication is SASL over TLS using SCRAM-SHA-512.
- **Federation.** Outbound connections to external OIDC identity providers use
  TLS and verify the provider's certificate, but negotiate whatever cipher and
  group the provider supports - the federation leg does not require CNSA ciphers,
  so interoperability with Google, Okta, Entra ID and ADFS is preserved.

### Managed-service caveat

Where a deployment fronts DFE with a managed load balancer or CDN (AWS
ALB/CloudFront, Azure, GCP), TLS policy is a **named bundle**, not an a-la-carte
suite - the closest policy usually also admits AES-128 and P-256. DFE's own
Envoy edge expresses the suite exactly; a managed front cannot. Deployments that
use one MUST pin the specific named policy and record the deltas from this
standard rather than assume a clean suite. Cloud KMS offers ECC P-384 for
signing/mTLS but no ML-KEM/ML-DSA keys yet, which is part of why the asymmetric
layer stays classical for now.

## Certificates

- **Leaf and CA certificates:** ECDSA on the **P-384** curve (CNSA 1.0
  classical), signed with SHA-384. Applies to the front-door wildcard
  certificate, the internal CA, and every in-cluster service certificate.
- **Issuance:** cert-manager issues in-cluster certificates from the P-384
  internal CA; public certificates come from the ACME issuer.
- **Lifetime and agility:** certificates are short-lived and auto-rotated.
  Pinning is deliberately avoided so the signature algorithm can advance
  (P-384 today, ML-DSA-87 when deployable) without re-pinning clients. Short
  lifetimes are the forge-later mitigation until ML-DSA is available.

## Data at rest

- **Secrets** are held in the secrets backend (OpenBao under Kubernetes; an
  encrypted file store in the single-container profile) and never written to
  disk in the clear. Any local secret cache is sealed with **AES-256-GCM**.
- **Signing keys.** The JWT signing private key is AES-256 wrapped at rest and
  released through the secrets seam.
- **Randomness.** Key material and tokens come from a CSPRNG at 256-bit strength
  or above.

## Hashing

SHA-384 is the DFE hash standard (SHA-512 equally acceptable); broker credential
hashing uses SCRAM-SHA-512. Password verification uses a work-hard KDF (bcrypt).
SHA-1 and MD5 are not used for any security purpose; where a downstream system
mandates a specific hash for non-security addressing (for example ClickHouse row
identity), that use is isolated from confidentiality and integrity.

## Tokens (JWT)

The JWT signature is **ES384** (ECDSA P-384 + SHA-384) - a deliberate, dated
decision to stay classical: ML-DSA in JOSE is still draft-stage with thin
library support, and an ML-DSA signature is far larger than a header/cookie
budget allows. ES384 is verifiable across the whole chain (Envoy `jwt_authn`,
PyJWT, Node `jose`). Two enforcement rules matter more than the curve:

- The accepted algorithm is **allow-listed to ES384 only** server-side. The
  classic JWT breaks are `alg:none` and RS/HS confusion, not the curve.
- ECDSA signing uses **RFC 6979 deterministic-k** (or a vetted library) - nonce
  reuse, not the curve choice, is ECDSA's failure mode.

## Post-quantum migration - the path to full CNSA 2.0

CNSA 2.0's asymmetric end-state is **ML-KEM-1024** (FIPS 203) for key
establishment and **ML-DSA-87** (FIPS 204) for signatures.

- **Key establishment (live today).** DFE offers the hybrid **X25519MLKEM768**
  group at the edge, which ships in mainstream TLS stacks (Chrome, Firefox,
  OpenSSL 3.5, Go 1.24, BoringSSL/aws-lc-rs) and gives real
  harvest-now-decrypt-later protection with no custom code. Two honest caveats:
  its post-quantum strength is **ML-KEM-768** (NIST level 3), below CNSA 2.0's
  ML-KEM-1024; and its classical half is **X25519**, not a NIST curve. The
  CNSA-pure hybrid is **SecP384r1MLKEM1024** (P-384 + ML-KEM-1024), which is
  specified but has far thinner deployment. DFE's conscious choice is to enable
  the widely-interoperable X25519MLKEM768 now for actual on-the-wire PQC
  protection, and to add SecP384r1MLKEM1024 as the preferred group once
  BoringSSL/Envoy support it - the key-exchange group is a config value, so this
  is a one-line change.
- **Signatures (roadmap).** ES384 is current across the chain. ML-DSA-87
  replaces it once its JOSE binding standardises and the verifiers (Envoy,
  server and browser libraries) support it. `kid`-tagged keys + config-driven
  `alg` make this a rotation, not a rewrite.

## Enforcement - policy-as-code, not prose

This standard is enforced in CI, not aspired to in a wiki. The deployment
pipeline gates on policy checks (conftest / checkov / tfsec over the helm +
terraform + ArgoCD manifests) so drift fails the build: every TLS listener sets
the profile floor, at-rest encryption is AES-256, and the internal mTLS profile
is a P-384 chain. A change that weakens the suite does not merge.

## References

- [NSA CNSA 1.0 (Suite B successor)](https://media.defense.gov/2021/Sep/27/2002862527/-1/-1/0/CNSS%20WORKSHEET.PDF) and
  [NSA CNSA 2.0 FAQ](https://media.defense.gov/2022/Sep/07/2003071836/-1/-1/0/CSI_CNSA_2.0_FAQ_.PDF)
- `AUTH-ENVOY-TOPOLOGY.md` (engine auth docs) - the ES384 JWT signing side.
- FIPS 203 (ML-KEM), FIPS 204 (ML-DSA), FIPS 197 (AES), FIPS 180-4 (SHA-2);
  `draft-ietf-tls-ecdhe-mlkem` (hybrid groups).
