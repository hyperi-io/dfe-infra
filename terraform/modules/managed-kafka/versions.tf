// This directory declares no resources. It holds the contract and the test
// files that assert it, one per body, each run block naming the body it
// exercises. The empty root exists because `tofu init` and `tofu test` need a
// configuration to run against, and this is what lets `tofu test` run every
// body's contract in one invocation.

terraform {
  required_version = ">= 1.10"
}
