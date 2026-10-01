# The baseline gp3 StorageClass for a fresh EKS cluster. Rendered by
# bootstrap.sh with envsubst, AWS only, and whenever DFE_STORAGE_CLASS does not
# already name a class on the cluster -- see bootstrap.sh step [1b/7]. EKS 1.30+
# ships gp2 with no default, and the aws-ebs-csi-driver add-on makes gp3
# POSSIBLE but creates no StorageClass object of its own, so the data pods' PVCs
# (Forgejo, ClickHouse, Keeper, CNPG) stay Pending until something creates one.
#
# DFE_STORAGE_CLASS_IS_DEFAULT is "true" only when the cluster has no default
# class at that point. A cluster that already has one keeps it: two default
# classes is a misconfiguration, and since Kubernetes 1.26 the admission plugin
# resolves it by taking the most recently created, which would silently move
# every unannotated PVC in the cluster -- not just DFE's -- onto this class.
#
# The free-tier baseline only -- 3,000 IOPS / 125 MiB/s, gp3's own floor, the
# same figures compute-shapes.yaml's aws.volume_baseline_iops and
# volume_baseline_throughput_mib_s name. A per-use-case class with the
# resolved iops/throughput (shapes/resolved/aws-<region>.json) is a follow-on;
# this class only has to exist so a bare cluster can bind a PVC.
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: "${DFE_STORAGE_CLASS}"
  annotations:
    storageclass.kubernetes.io/is-default-class: "${DFE_STORAGE_CLASS_IS_DEFAULT}"
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  iops: "3000"
  throughput: "125"
  # The account's default EBS key. A deployment that encrypts with its own
  # KMS key must grant the aws-ebs-csi-driver add-on's role kms:CreateGrant
  # (and the usual Decrypt/GenerateDataKey) on that key before volumes using
  # it will provision.
  encrypted: "true"
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
reclaimPolicy: Delete
