# Secrets Setup Guide

Helm is the only supported deployment path, alpha through production (see
[`QUICKSTART.md`](QUICKSTART.md#deploy-with-helm)) — secrets are supplied through Helm for every
environment. Never commit real credentials into a values file.

## Local/Alpha

For a quick local install, generate strong secrets and pass them with `--set`:

```bash
SECRET_KEY=$(openssl rand -hex 32)   # session-cookie / refresh-token MAC key; NOT a JWT key
DB_PASS=$(openssl rand -hex 16)
REDIS_PASS=$(openssl rand -hex 16)

helm install waddlebot ./k8s/helm/waddlebot -n waddlebot --create-namespace \
  --kube-context local-alpha \
  -f k8s/helm/waddlebot/values-alpha.yaml \
  --set global.secretKey="$SECRET_KEY" \
  --set secrets.dbPassword="$DB_PASS" \
  --set secrets.redisPassword="$REDIS_PASS"
```

Or create a `values-secrets.yaml` file (gitignored, never committed) with real values and pass it
alongside the environment values file:

```bash
helm install waddlebot ./k8s/helm/waddlebot -n waddlebot --create-namespace \
  -f k8s/helm/waddlebot/values-alpha.yaml \
  -f values-secrets.yaml
```

## Beta/Production

Use a proper secrets mechanism instead of `--set`/plaintext values files — Vault, Sealed Secrets,
or External Secrets Operator, feeding `k8s/helm/waddlebot/templates/secrets.yaml`:

```bash
helm upgrade waddlebot ./k8s/helm/waddlebot -n waddlebot \
  --kube-context dal2-beta \
  -f k8s/helm/waddlebot/values-beta.yaml
```

See [`CREDENTIALS-ROTATION-CHECKLIST.md`](CREDENTIALS-ROTATION-CHECKLIST.md) for what to rotate
and how.
