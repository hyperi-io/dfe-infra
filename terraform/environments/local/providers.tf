# Only the openbao backend plans a vault object, so with none an empty address is
# never dialled. A null token leaves the provider's own VAULT_TOKEN and
# ~/.vault-token lookup.
provider "vault" {
  address = var.vault_addr
  token   = var.vault_token != "" ? var.vault_token : null
}
