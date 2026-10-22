# dal2-alpha apply order (all `--context dal2alpha`; nothing here is auto-applied)

## 0. HARD PREREQUISITE (before ANY step below)
Node-IP host fix: set kubelet `--node-ip` to 192.168.8.41 / 192.168.8.51 on the respective nodes,
restart kubelet + cilium, then confirm `cilium status` shows 2/2 ready. Until then cert-manager's
webhook and cross-node traffic are down and everything below will fail.

```bash
kubectl --context dal2alpha get nodes -o wide      # INTERNAL-IP = .41/.51
cilium status --context dal2alpha                  # 2/2
```

## 1. Shared Gateway
```bash
kubectl --context dal2alpha apply -f k8s/dal2alpha/gateway.yaml
kubectl --context dal2alpha -n gateway-system get gateway shared svc   # NodePort is dynamic
```
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
~/.local/bin/helm3 --kube-context dal2alpha upgrade --install waddlebot k8s/helm/waddlebot \
  -n waddlebot -f k8s/helm/waddlebot/values-dal2alpha.yaml
```
Then point the 443 NAT at the Gateway NodePort from step 1.
