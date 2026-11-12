# Node autoscaling for on-premises clusters

KEDA adds pods, and a pod with no room Pends until something adds a node. [Autoscaling](../autoscaling.md) sets out the two layers and the per-target choice. On AWS the node layer is Karpenter, through the node-pools chart's `karpenter-aws` renderer.

On-premises DFE ships no node autoscaler. The baseline is a fixed RKE2 node pool, so capacity planning replaces node autoscaling, and KEDA still scales pods inside the nodes that exist. Size the pool for the peak, because a pod KEDA adds past it stays Pending. Karpenter isn't an option there, because it needs a cloud provisioning API to mint machines.

Elastic on-premises nodes aren't in this release. The shape they'd take is Cluster API (CAPI) holding the workers as `MachineDeployment` objects, with the CAPI-aware Cluster Autoscaler moving the replica count when pods Pend.
