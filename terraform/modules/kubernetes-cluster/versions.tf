// This directory declares no resources. It holds the contract, and the shared
// test file that every body under it must pass -- each run block names the body
// it exercises. The empty root exists because `tofu init` and `tofu test` need
// a configuration to run against, and this is what makes one body's test the
// same file as the next one's.

terraform {
  required_version = ">= 1.10"
}
