<!--
Project:   DFE (Data Forensics Engine) - product suite
File:      docs/CNSA-STANDARD.md
Purpose:   DFE cryptographic standard at the deployment layer - transport,
           certificate and at-rest crypto - aligned to CNSA 2.0.
Language:  Markdown
License:   see repository LICENSE
Copyright: HyperI / DFE contributors
-->

# DFE cryptographic standard (CNSA 2.0)

DFE is engineered for **CNSA 2.0** (the NSA Commercial National Security
Algorithm Suite 2.0), because national-security deployment is in scope. The user
base is small and high-value, so algorithm strength is chosen for compliance
rather than throughput - signing and cipher CPU cost is not a constraint.

This document is the deployment-layer crypto standard: **transport (TLS),
certificates, and data at rest**. The application-signing side (the ES384 JWT
that authenticates every request) is covered by the engine's auth topology
document (`AUTH-ENVOY-TOPOLOGY.md`); the two together describe the whole
cryptographic posture.

## Profiles

DFE ships two operating profiles. A deployment selects one; every component
inherits the profile's algorithm floor.

| Layer | `prod` (default) | `highsec` (full CNSA 2.0) |
|---|---|---|
| TLS | 1.3 only | 1.3 only |
| Symmetric | AES-256-GCM | AES-256-GCM |
| Hashing | SHA-384 | SHA-384 / SHA-512 |
| Certificates / signatures | ECDSA P-384, SHA-384 | ECDSA P-384 -> ML-DSA-87 |
| Key establishment | ECDHE P-384, hybrid ML-KEM where available | ML-KEM-1024 (hybrid) |
| SSH | Ed25519, ML-KEM hybrid KEX | Ed25519, ML-KEM hybrid KEX |

`prod` is the corporate default and satisfies CNSA 2.0's classical /
transitional profile. `highsec` is the post-quantum end-state, adopted as the
underlying libraries, TLS stacks and JOSE bindings mature. The design is
**crypto-agile** throughout - protocol floors, curves, cipher policy and signing
algorithms are configuration, not code, so the transition is a config change.

## Transport (TLS)

All DFE network transport is TLS 1.3, AES-256-GCM, SHA-384, ECDHE P-384.

- **Edge.** The single Envoy front layer terminates TLS 1.3 only, offers
  `TLS_AES_256_GCM_SHA384` (with `TLS_CHACHA20_POLY1305_SHA256` as an
  ARM/mobile fallback), and negotiates ECDHE on P-384. Where the TLS stack
  provides it, the edge also offers the hybrid **X25519MLKEM768** key-exchange
  group - the CNSA 2.0 key-establishment step that is feasible today - so a
  post-quantum-capable client gets post-quantum key agreement with no protocol
  break for classical clients.
- **Backing services.** ClickHouse, Kafka, the metadata store and the telemetry
  pipeline all speak TLS 1.3 with verified certificates issued by the internal
  certificate authority. Broker authentication is SASL over TLS using
  SCRAM-SHA-512.
- **Federation.** Outbound connections to external OIDC identity providers use
  TLS 1.3 and verify the provider's certificate, but negotiate the cipher and
  key-exchange group the provider supports - the federation leg does not require
  CNSA-only ciphers, so interoperability with Google, Okta, Entra ID and ADFS is
  preserved.

## Certificates

- **Leaf and CA certificates:** ECDSA on the **P-384** curve, signed with
  SHA-384. This applies to the front-door wildcard certificate, the internal
  certificate authority, and every service certificate issued in-cluster.
- **Issuance:** cert-manager issues in-cluster certificates from a P-384
  internal CA; public-facing certificates come from the ACME issuer with a
  P-384 account key.
- **Lifetime and agility:** certificates are short-lived and automatically
  rotated. Certificate pinning is deliberately avoided so the signing algorithm
  can advance (P-384 today, ML-DSA-87 on the CNSA 2.0 timeline) without
  re-pinning clients.

## Data at rest

- **Secrets.** Secrets are held in the secrets backend (OpenBao under
  Kubernetes; an encrypted file store in the single-container profile) and are
  never written to disk in the clear. Any local secret cache is sealed with
  **AES-256-GCM**.
- **Signing keys.** The JWT signing private key is AES-256 wrapped at rest and
  released to the engine through the secrets seam.
- **Randomness.** All key material and tokens are generated from a
  cryptographically secure random source at 256-bit strength or above.

## Hashing

SHA-384 is the DFE hash standard, matching the P-384 security level. SHA-512 is
equally acceptable. Broker credential hashing uses SCRAM-SHA-512. Password
verification uses a memory-or-work-hard KDF (bcrypt). SHA-1 and MD5 are not used
for any security purpose; where a downstream system mandates a specific hash for
non-security addressing (for example ClickHouse row identity), that usage is
isolated and does not bear on confidentiality or integrity.

## Post-quantum roadmap

CNSA 2.0's post-quantum end-state is **ML-KEM-1024** (FIPS 203) for key
establishment and **ML-DSA-87** (FIPS 204) for signatures. DFE adopts each as
the ecosystem support lands:

- **Key establishment:** hybrid **X25519MLKEM768** is offered at the edge today
  wherever the TLS stack supports it; the full ML-KEM-1024 profile follows as
  library support matures.
- **Signatures:** ES384 is the current signing algorithm across the whole
  verification chain. ML-DSA-87 replaces it once its JOSE binding is
  standardised and the verifiers (TLS proxy, server and browser libraries)
  support it. Because keys are `kid`-tagged and the algorithm is
  configuration-driven, this is a rotation, not a rewrite.

## References

- [NSA CNSA 2.0 FAQ](https://media.defense.gov/2022/Sep/07/2003071836/-1/-1/0/CSI_CNSA_2.0_FAQ_.PDF)
- `AUTH-ENVOY-TOPOLOGY.md` (engine auth docs) - the application-signing
  (ES384 JWT) side of the posture.
- FIPS 203 (ML-KEM), FIPS 204 (ML-DSA), FIPS 197 (AES), FIPS 180-4 (SHA-2).
