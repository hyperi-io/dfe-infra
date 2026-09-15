// One zone here, and the split is the private-by-default principle in DNS form.
// Everything DFE talks to internally resolves in this PRIVATE zone, inside the
// VPC only. The public zone, external-dns's identity and cert-manager's DNS-01
// identity all belong to the edge module (terraform/modules/edge/aws/dns.tf),
// because they exist only so something outside the VPC can reach in.

resource "aws_route53_zone" "private" {
  name = var.dns.private_zone

  vpc {
    vpc_id = aws_vpc.this.id
  }
}

// external-dns runs policy: sync (argocd/appsets/layer1-addons.yaml), so a
// deleted Service or Ingress gets its record removed while the controller is
// still alive to react. Sync cannot cover `tofu destroy` itself: the EKS
// cluster and every node go down in the same run, so whatever external-dns
// last published is still in the zone with no controller left to remove it,
// and Route 53 refuses to delete a hosted zone that still holds a record
// beyond its own apex NS/SOA pair.
//
// A destroy-time provisioner's command may reference only self, count.index
// or each.key -- OpenTofu's own words: "The command field do[es] support
// references only the self object, each.key and count.index, which
// represents the information that OpenTofu is still having in the state
// about the targeted resource" (opentofu.org/docs/language/resources/
// syntax/). So the zone id and region are captured into this resource's own
// input first, and the provisioner reads them back off self.output.
//
// Referencing aws_route53_zone.private.zone_id in that input is what orders
// this resource's destroy ahead of the zone's: OpenTofu "treats those
// references as implicit ordering requirements when creating, updating, or
// destroying resources" (opentofu.org/docs/language/resources/behavior/),
// and a resource that references another is created after it and destroyed
// before it -- the same reason the destroy graph splits create and destroy
// nodes at all, "because the destroy order is often different from the
// create order" (opentofu.org/docs/internals/graph/).
resource "terraform_data" "private_zone_teardown" {
  input = {
    zone_id = aws_route53_zone.private.zone_id
    region  = var.provision.region
  }

  // A zone replacement (the private_zone name changing) is exactly the case
  // that needs the OLD zone emptied before the OLD terraform_data instance
  // itself is replaced, so the trigger is the id whose change means "this
  // zone is going away," not a value that only changes when nothing does.
  triggers_replace = [aws_route53_zone.private.zone_id]

  // Runs under whatever AWS identity is already authenticated for `tofu
  // destroy` -- the same one that created the zone -- so it carries no IAM
  // of its own. Deletes every record but the apex NS/SOA pair Route 53
  // manages itself, which is exactly what ChangeResourceRecordSets requires
  // to be true before the zone can be deleted. --query does the filtering
  // and reshaping so the only external dependency is the aws CLI already
  // needed to authenticate this destroy, not a second tool.
  provisioner "local-exec" {
    when = destroy
    // A zone already emptied or already deleted by hand must not wedge the
    // destroy: whatever this cannot clear still surfaces as the zone's own
    // HostedZoneNotEmpty, which names the remaining records.
    on_failure = continue

    command = <<-EOT
      set -eu
      zone="${self.output.zone_id}"
      region="${self.output.region}"
      batch="$(aws route53 list-resource-record-sets \
        --hosted-zone-id "$zone" --region "$region" --output json \
        --query "{Changes: ResourceRecordSets[?Type!='NS' && Type!='SOA'].{Action: 'DELETE', ResourceRecordSet: @}}")"
      case "$batch" in
        *'"Action"'*)
          aws route53 change-resource-record-sets \
            --hosted-zone-id "$zone" --region "$region" --change-batch "$batch"
          ;;
      esac
    EOT
  }
}
