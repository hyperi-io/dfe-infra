# Trusting the DFE certificate

Which CA signed the gateway's `*.<domain>` certificate is a per-deployment
choice, and the two modes ask different things of a developer box. One command
says which is live, and the readiness gate and access summary print it too:

    python3 scripts/dfe-ops ca --status --kubeconfig .tmp/kubeconfig-dfe-b

Release and deploy helpers around this one live in
[DEPLOY-HELPERS.md](DEPLOY-HELPERS.md).

## Self-signed mode (the product default)

`tls.issuerName: dfe-internal-ca` -- cert-manager mints a `CN=dfe-internal-ca`
root (ECDSA P-384, ten years) and its ClusterIssuer signs the edge wildcard. No
DNS-01, no external credential, and it works on a split-horizon name public ACME
cannot validate. The cost is trust: a browser warns, and the HyperDX iframe
fails outright because it cannot show the interstitial. Trust the root once:

    python3 scripts/dfe-ops ca             # the PEM plus the install lines
    python3 scripts/dfe-ops ca --install   # writes the file, does the non-root half

`--install` writes `.tmp/dfe-internal-ca.crt` and branches on
`platform.system()`. On Linux it adds the certificate to the Chromium-family NSS
store at `~/.pki/nssdb` (Brave and Chrome read that, not the system store; needs
`libnss3-tools`) and prints `sudo cp <file>
/usr/local/share/ca-certificates/` plus `sudo update-ca-certificates`. On macOS
Chrome and Safari read the system keychain, so nothing runs without root and the
one line printed is `sudo security add-trusted-cert -d -r trustRoot -k
/Library/Keychains/System.keychain <file>`. Plain `dfe-ops ca` prints both.

**The root PERSISTS across a rebuild** (#238), so that trust is a one-off. The
gateway chart pushes the minted root to the deployment's secret store once
(`PushSecret`, `updatePolicy: IfNotExists`) and restores it before cert-manager
can mint (`ExternalSecret`, `refreshPolicy: CreatedOnce`); bootstrap.sh renders
that same pair ahead of Argo, so the restore is ordered before the Certificate
rather than racing it. The root is reused; the leaf lifetimes rotate.

| Setting | Where | Effect |
|---|---|---|
| `tls.internalCA.persist.enabled` | gateway chart values | **Off by default.** An ExternalSecret against a store the deployment does not have is Degraded forever, which fails the Argo sync of a working deploy. Turn it on where there is a store. |
| `tls.internalCA.persist.secretStoreName` | gateway chart values | The store both halves use (default `dfe-secret-store`); empty renders neither. |
| `DFE_CA_PERSIST` | bootstrap env | Whether bootstrap pre-applies the restore. Defaults on when `DFE_VAULT_SECRET_ID` is set. |
| `DFE_CA_RESTORE_TIMEOUT` | bootstrap env | Seconds to wait for the restore (default 60). A first bootstrap times out by design. |

The store path is `<project>/<env>/pki/internal-ca`, properties `tls_crt` and
`tls_key` -- underscored, because the Vault provider reads a property as a gjson
path and a dot means nesting.

## Estate-PKI mode (`tls.vault`)

In an estate that already runs a PKI, point the edge issuer at it and every
client trusts the certificate already -- nothing to install, no root to persist.
Set it in the deploy repo's own overlay (`infra/envoy-gateway-config.yaml`),
never in a tracked dfe-infra values file:

```yaml
tls:
  issuerName: dfe-estate-pki
  vault:
    server: https://vault.example.com:8200
    path: pki_tls/sign/<role>          # the sign role the AppRole policy grants
    caBundle: <base64 PEM chain that verifies the server's own TLS>
    appRole:
      roleId: <the cert-manager AppRole role_id>
```

Bootstrap seeds the AppRole SecretID from `DFE_CERTMANAGER_SECRET_ID` into
Secret `cert-manager-approle` (key `secretId`), namespace `cert-manager`. The
sign role must allow the deployment's wildcard and match `tls.privateKey` (ECDSA
P-384 by default). `tls.acme.email` and `tls.vault.server` are exclusive; the
chart fails the render on both.

Three things about the values that are easy to get wrong:

- `issuerName` must NOT be `tls.internalCA.issuerName` -- that name already
  belongs to the internal mesh CA's ClusterIssuer, and `dfe-ops ca --status`
  reads the wildcard's `issuerRef` against it to decide the mode.
- `caBundle` is the chain that verifies the PKI SERVER's own HTTPS, not the CA
  the PKI issues from. Take it off the handshake:
  `openssl s_client -connect <host>:<port> -showcerts`, keep every certificate
  after the leaf, base64 the concatenation onto one line.
- Nothing needs deleting to flip an existing deployment. The gateway-shim owns
  the `dfe-wildcard-tls` Certificate, so changing `tls.issuerName` rewrites its
  `issuerRef` and cert-manager re-issues in place; the new leaf's `notBefore`
  is the proof it went round again.

Vault signs from the CSR's SANs, so the leaf carries an empty subject and a
critical `subjectAltName`. That is correct, not a truncated certificate.
