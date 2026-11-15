# ...and if it's not DNS, it's PKI

The name resolves, the port answers, and the connection still dies in the handshake. A certificate fails three ways. It expires, the CA that signed it changes or goes missing, or the name you dialled isn't in it. DFE components mostly can't tell you which, so ask the certificate.

From a pod or container on the same network as the failing client, print what the server presents and whether a CA file trusts it. The engine image carries `openssl`.

```bash
# what the server presents
openssl s_client -connect <host>:<port> -servername <host> </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates -ext subjectAltName

# whether this CA file trusts it for this name
openssl s_client -connect <host>:<port> -servername <host> -CAfile <ca.crt> \
  -verify_hostname <host> </dev/null 2>&1 | grep -E '^Verification|Verify return'
```

On a cluster, list every certificate cert-manager owns with its expiry and its renewal time:

```bash
kubectl get certificates -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,READY:.status.conditions[0].status,NOTAFTER:.status.notAfter,RENEW:.status.renewalTime
python3 scripts/dfe-ops ca --status
```

`dfe-ops ca --status` names the signing CA and, on a deployment that doesn't persist its root, says `the next rebuild mints another root`. A rebuild is a CA that goes away. Every client holding the old root then fails until it gets the new one, as [deploy-tls-trust.md](deploy-tls-trust.md) describes.

## What each fault looks like

Each row is one fault against a real client, with the server presenting a certificate from a throwaway CA.

| Fault | `openssl s_client` | DFE client |
|---|---|---|
| Expired leaf | `Verification error: certificate has expired` | dfe-loader: `ClickHouse refused the startup probe, staying not ready ... network error: client error (Connect)`. dfe-hyperdx: `MongooseServerSelectionError: certificate has expired`. |
| CA not trusted, or replaced | `Verification error: unable to get local issuer certificate`. `curl` exits 60 with `SSL certificate problem: unable to get local issuer certificate`. | dfe-loader: the same `client error (Connect)` line. |
| Name not in the certificate | `Verification error: hostname mismatch` | dfe-loader: the same `client error (Connect)` line. |
| CA file replaced under a running container | | dfe-hyperdx: `ENOENT: no such file or directory, open '/etc/dfe/documentdb-ca/ca.crt'` |

The loader prints the same line for an unresolvable name and for all three certificate faults. At debug level the expired case logged `connecting to <address>:8443` and `connected to` before the warning, and the unresolvable name logged neither. So a `connected to` line points at the handshake. The loader printed no certificate text at info or debug in those runs, which is why `openssl s_client` is the tool.

A loader with a good certificate logged no warning. A loader with any of the three faults stays not ready, retrying with growing delays.

The health checks of a service whose client has the fault stay green. With an expired certificate on DocumentDB, both `dfe-hyperdx-documentdb` and `dfe-hyperdx` reported `healthy` while every connection failed.

## Renewals that can't happen

On a cluster, cert-manager renews a certificate when its renewal time arrives, as long as the issuer is ready. Take the issuer's CA away and renewal stops, but `kubectl get certificate` keeps saying `READY True`.

A CA issuer whose `secretName` points at a Secret that doesn't exist reports:

```text
Ready=False reason=ErrGetKeyPair msg=Error getting keypair for CA issuer: secrets "<name>" not found
```

At the certificate's renewal time the Certificate stayed `Ready=True` and gained a second condition, `Issuing=True reason=Renewing`. The new CertificateRequest sat unsigned:

```text
Ready=False reason=Pending msg=Referenced issuer does not have a Ready status condition
```

So READY on a Certificate proves nothing before expiry. Look at the requests and the issuers instead:

```bash
kubectl get certificaterequest -A        # READY False is a renewal that is stuck
kubectl get issuer,clusterissuer -A      # READY False is the cause
kubectl describe issuer <name> -n <namespace>
```

The fix is to give the issuer its CA back. Restore the Secret under the name the issuer references, from the persisted root or a backup, then check that `kubectl get issuer` shows `READY True` and that the pending CertificateRequest signs. The pods that serve the certificate then need a restart.

A renewed certificate isn't served until the process reloads it. After a renewal the mounted file held the new serial while a server that had started earlier kept presenting the old one, which `openssl s_client` showed by its different serial. Restart the pods that serve the certificate, or give them a reloader, once the Secret has changed.

On Docker Compose the stack makes its own CA. It generates a CA and a DocumentDB server certificate for dfe-hyperdx in the `dfe-hyperdx-documentdb-tls` one-shot, and regenerates both when the leaf is within 30 days of expiry. Running `docker compose up -d` against a stack whose leaf had expired re-ran that one-shot and wrote a new CA and leaf, but left the running containers alone. DocumentDB kept serving the old leaf and dfe-hyperdx lost the path to its CA file. The result was `Verification error: unable to get local issuer certificate` from the DocumentDB port and `ENOENT ... /etc/dfe/documentdb-ca/ca.crt` in dfe-hyperdx.

Restart them in this order, database first:

```bash
docker restart dfe-hyperdx-documentdb
docker restart dfe-hyperdx
```

The handshake then verified (`Verification: OK`) and dfe-hyperdx logged `Connection established to MongoDB` on its next minute.
