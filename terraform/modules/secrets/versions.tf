// This directory declares no resources. It holds the contract and the test
// file that asserts the aws-sm body's deletion-protection knob. The empty
// root exists because `tofu init` and `tofu test` need a configuration to
// run against.

terraform {
  required_version = ">= 1.10"
}
