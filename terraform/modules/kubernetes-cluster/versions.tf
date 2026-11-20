// Declares no resources: it holds the contract and the shared test every body must pass. The
// empty root exists because `tofu init` and `tofu test` need a configuration to run against.

terraform {
  required_version = ">= 1.10"
}
