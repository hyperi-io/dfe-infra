# MetalLB front-door pool -- the two addresses this deployment publishes, so a
# rebuild lands on the same IPs the DNS records already point at.
# Rendered by bootstrap.sh with envsubst, on-prem only (DFE_CLOUD=local).
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: dfe-front-door
  namespace: metallb-system
spec:
  # /32 each: the pool holds these two addresses and nothing adjacent to them.
  addresses:
    - "${DFE_GATEWAY_IP}/32"
    - "${DFE_RECEIVER_IP}/32"
  # Both Services ask for their address by name, and an automatic assignment
  # would let whichever Service asked first take the other one's.
  autoAssign: false
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: dfe-front-door
  namespace: metallb-system
spec:
  # Layer 2 (ARP/NDP) rather than BGP: the nodes and the addresses share one
  # VLAN, so no router peering is involved.
  ipAddressPools:
    - dfe-front-door
