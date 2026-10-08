locals {
  // timeadd takes a Go duration, so epoch seconds become an offset from the epoch.
  expiry_iso8601 = var.run == null ? "" : formatdate(
    "YYYY-MM-DD'T'hh:mm:ss'Z'",
    timeadd("1970-01-01T00:00:00Z", format("%ds", var.run.expires_at)),
  )
}

output "tags" {
  description = "The run's two tags, to merge into the provider's default_tags (aws), default_labels (google) or each resource's tags (azurerm). Empty when run is null, so a deployment that is not a test run carries no expiry a reaper could act on."
  value = var.run == null ? tomap({}) : tomap({
    (var.run.keys.run)    = var.run.id
    (var.run.keys.expiry) = var.run.keys.format == "epoch" ? tostring(var.run.expires_at) : local.expiry_iso8601
  })
}
