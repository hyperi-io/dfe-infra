// One PRIVATE zone, resolving inside the VPC only. The public zone and the identities that write
// it (external-dns, cert-manager DNS-01) belong to the edge module
// (terraform/modules/edge/aws/dns.tf), because they exist only so something outside can reach in.

resource "aws_route53_zone" "private" {
  name = var.dns.private_zone

  vpc {
    vpc_id = aws_vpc.this.id
  }
}

// external-dns runs policy: sync, which cannot cover `tofu destroy`: the cluster and every node
// go down in the same run, leaving the last published records in a zone Route 53 will not
// delete while it holds anything beyond its apex NS/SOA pair. This empties it first
// (docs/deployment/aws-configuration.md, external-dns).
//
// A destroy-time provisioner may reference only self, so the zone id and region are captured into
// this resource's input and read back from self.output. Referencing the zone's id also orders
// this resource's destroy ahead of the zone's.
resource "terraform_data" "private_zone_teardown" {
  input = {
    zone_id = aws_route53_zone.private.zone_id
    region  = var.provision.region
    // The apex name, so the filter below spares the apex NS/SOA pair alone. A delegation's own NS
    // record is not the apex's and has to go.
    apex = aws_route53_zone.private.name
  }

  // A zone replacement empties the OLD zone before the old instance is replaced, so the trigger is the zone id.
  triggers_replace = [aws_route53_zone.private.zone_id]

  // Runs under the AWS identity already authenticated for `tofu destroy`, so it has no IAM of its
  // own. --query does the filtering, so the aws CLI the destroy already needs is the only
  // dependency, and the CLI auto-paginates so the filter sees every record.
  //
  // One batch, and ChangeResourceRecordSets caps one at 1000 changes: a bigger zone is rejected
  // whole and falls through to the zone's own HostedZoneNotEmpty.
  provisioner "local-exec" {
    when = destroy
    // A zone already emptied or deleted by hand must not fail the destroy: what this cannot clear
    // still surfaces as the zone's HostedZoneNotEmpty, which names the remaining records.
    on_failure = continue

    command = <<-EOT
      set -eu
      zone="${self.output.zone_id}"
      region="${self.output.region}"
      apex="${self.output.apex}"
      apex="$${apex%.}."
      batch="$(aws route53 list-resource-record-sets \
        --hosted-zone-id "$zone" --region "$region" --output json \
        --query "{Changes: ResourceRecordSets[?!(Name=='$apex' && (Type=='NS' || Type=='SOA'))].{Action: 'DELETE', ResourceRecordSet: @}}")"
      case "$batch" in
        *'"Action"'*)
          aws route53 change-resource-record-sets \
            --hosted-zone-id "$zone" --region "$region" --change-batch "$batch"
          ;;
      esac
    EOT
  }
}
