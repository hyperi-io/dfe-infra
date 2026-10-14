terraform {
  // 1.9 is the first OpenTofu whose variable validations may read other
  // variables and locals, which every refusal in variables.tf does.
  required_version = ">= 1.9"
}
