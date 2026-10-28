# dal2-alpha apply order (all `--context dal2alpha`; nothing here is auto-applied)

## 0. HARD PREREQUISITE (before ANY step below)
Node-IP host fix: set kubelet `--node-ip` to 192.168.8.41 / 192.168.8.51 on the respective nodes,
restart kubelet + cilium, then confirm `cilium status` shows 2/2 ready. Until then cert-manager's
webhook and cross-node traffic are down and everything below will fail.

```bash
kubectl --context dal2alpha get nodes -o wide      # INTERNAL-IP = .41/.51
cilium status --context dal2alpha                  # 2/2
```
L2 announce (the LB VIP below) needs Cilium cross-node health good: each node's INTERNAL-IP must
be its eth0 LAN IP, otherwise lease election/ARP announce is unreliable.

### 0b. Cilium agent prerequisites for the LB VIP (verify BEFORE step 1)
Cilium LB IPAM + L2 announcements replace MetalLB -- do NOT install MetalLB. L2 announcements are
off by default and need kube-proxy replacement (already required by Gateway API):
```bash
kubectl --context dal2alpha -n kube-system get cm cilium-config -o yaml \
  | grep -E 'enable-l2-announcements|kube-proxy-replacement|^\s+devices'
# need: enable-l2-announcements: "true", kube-proxy-replacement: true/"true", and (if `devices`
# is set explicitly) it must include eth0. If not enabled, upgrade Cilium (keep existing values):
#   helm upgrade cilium cilium/cilium -n kube-system --reuse-values \
#     --set l2announcements.enabled=true --set kubeProxyReplacement=true \
#     --set k8sClientRateLimit.qps=<QPS> --set k8sClientRateLimit.burst=<BURST>
# (size qps/burst per https://docs.cilium.io/en/stable/network/l2-announcements/ -- L2 leases add
#  API load), then `kubectl -n kube-system rollout restart ds/cilium deploy/cilium-operator`.
```
Reserve **192.168.8.60** on the router: outside the DHCP range, not used by any other host.

## 1. Shared Gateway
```bash
kubectl --context dal2alpha apply -f k8s/dal2alpha/gateway.yaml
kubectl --context dal2alpha -n gateway-system get gateway shared svc   # svc EXTERNAL-IP = 192.168.8.60
```
Also applies the LB VIP objects: `CiliumLoadBalancerIPPool dal2alpha-gateway-vip` (single IP
192.168.8.60/32, scoped to Service gateway-system/cilium-gateway-shared only) and
`CiliumL2AnnouncementPolicy dal2alpha-gateway-l2` (ARP on `^eth0$`, both nodes, one lease holder
at a time, automatic failover). The Gateway requests .60 via `spec.addresses`; if it can't be
assigned the Gateway shows Programmed=False / AddressNotUsable.
```bash
kubectl --context dal2alpha get ciliumloadbalancerippool dal2alpha-gateway-vip   # CONFLICTING=False, IPS AVAILABLE=0 (1 in use)
kubectl --context dal2alpha -n kube-system get lease | grep l2announce            # lease cilium-l2announce-gateway-system-cilium-gateway-shared; holderIdentity = announcing node
# from a LAN host: arping -I <lan-if> -c 3 192.168.8.60   (answers with the holder node's MAC)
```
This also applies `CiliumClusterwideNetworkPolicy shared-gateway-cloudflare-only`: the gateway
accepts :443 ONLY from Cloudflare IP ranges. Verify post-apply that Cloudflare-proxied requests
still pass and a direct hit to the origin IP is dropped (adversarial review required first).
Also confirm the policy still sees the real Cloudflare client IP via the VIP (the router must
port-forward/DNAT only, NOT SNAT/masquerade inbound; Service keeps externalTrafficPolicy Cluster
because L2 announce is incompatible with Local).
- **Refresh Cloudflare ranges** (they change): diff https://www.cloudflare.com/ips-v4 and /ips-v6
  against the CIDRs in gateway.yaml, update, re-apply. Do this at least quarterly.
- **Follow-up (stronger): Cloudflare Authenticated Origin Pulls** -- enable AOP in the CF zone and
  make the 443 listener require Cloudflare's client cert (Gateway `tls.frontend` client-cert
  validation with the CF origin-pull CA). Needs CF-side enablement; the IP allowlist is the
  interim self-contained control.
- **Route attachment gate**: Gateway only accepts HTTPRoutes from namespaces labeled
  `gateway-access=shared-dal2a`. Any new app namespace must be labeled by the platform admin.

Gateway stays not-Programmed until step 4 creates the TLS secret.

## 2. Clear failed cert-manager startup check
```bash
kubectl --context dal2alpha -n cert-manager delete job cert-manager-startupapicheck --ignore-not-found
kubectl --context dal2alpha -n cert-manager rollout status deploy/cert-manager-webhook
```

## 3. Internal CA
```bash
kubectl --context dal2alpha apply -f k8s/dal2alpha/internal-ca.yaml
kubectl --context dal2alpha -n cert-manager wait certificate/waddlebot-internal-ca --for=condition=Ready --timeout=120s
kubectl --context dal2alpha get clusterissuer waddlebot-internal-ca
# CA carries critical nameConstraints (waddlebot.svc.cluster.local, penguintech.io URIs only)
```

## 4. Cloudflare Origin wildcard TLS secret (key/cert from files, never CLI literals)
```bash
kubectl --context dal2alpha -n gateway-system create secret tls cloudflare-origin-wildcard \
  --cert=/secure/path/origin.pem --key=/secure/path/origin.key
kubectl --context dal2alpha -n gateway-system get gateway shared   # Programmed=True
```

## 5. Waddles (helm 3.17.x -- NOT v4, helm#31361 fresh-install bug)
Prereq secrets in ns waddlebot: `ghcr-pull`, `waddlebot-platform-credentials`.
```bash
kubectl --context dal2alpha create ns waddlebot --dry-run=client -o yaml | kubectl --context dal2alpha apply -f -
# REQUIRED: without this label the shared Gateway rejects the waddlebot HTTPRoute
kubectl --context dal2alpha label ns waddlebot gateway-access=shared-dal2a --overwrite
~/.local/bin/helm3 --kube-context dal2alpha upgrade --install waddlebot k8s/helm/waddlebot \
  -n waddlebot -f k8s/helm/waddlebot/values-dal2alpha.yaml
```
Then set the router NAT: **`public:443 -> 192.168.8.60:443`** (the Cilium LB VIP from step 1,
announced over L2/ARP on eth0). It is NOT a NodePort and NOT a node IP -- the VIP survives a node
loss (the other node takes the L2 lease), whereas pointing at .41 or .51 would not.
