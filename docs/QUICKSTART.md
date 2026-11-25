# Waddles Quick Start Guide

Deploy Waddles with Helm and get a community connected in one pass. Docker Compose is not
supported — Kubernetes via Helm is the only deployment path, alpha through production.

## Prerequisites

### Required tooling

- **Kubernetes cluster**: MicroK8s or Docker Desktop for local/alpha; a managed cluster for beta/prod
- **kubectl**, configured with a context for your cluster
- **Helm 3**
- **Git**

### Minimum system requirements (local/alpha)

- **CPU**: 4 cores
- **RAM**: 8 GB minimum, 16 GB recommended
- **Disk Space**: 50 GB minimum

### Platform API credentials (optional for first run)

To connect Waddles to a platform, you'll need:

- **Twitch**: App ID, App Secret, Webhook Secret ([Create Twitch App](https://dev.twitch.tv/console/apps))
- **Discord**: Bot Token, Application ID, Public Key ([Create Discord App](https://discord.com/developers/applications))
- **Slack**: Bot Token, App Token, Signing Secret ([Create Slack App](https://api.slack.com/apps))
- **YouTube**: API Key, Client ID, Client Secret ([Google Cloud Console](https://console.cloud.google.com/))

## Deploy with Helm

### Step 1: Clone the repository

```bash
git clone https://github.com/penguintechinc/waddles.git
cd waddles
```

### Step 2: Install

```bash
# Local / alpha (MicroK8s or Docker Desktop)
helm install waddlebot ./k8s/helm/waddlebot -n waddlebot --create-namespace \
  --kube-context local-alpha \
  -f k8s/helm/waddlebot/values-alpha.yaml

# Beta
helm install waddlebot ./k8s/helm/waddlebot -n waddlebot --create-namespace \
  --kube-context dal2-beta \
  -f k8s/helm/waddlebot/values-beta.yaml
```

Secrets (database password, JWT signing key, platform OAuth credentials) are supplied via your
cluster's secrets mechanism (Vault, Sealed Secrets, or External Secrets Operator) — never edit
plaintext values into a values file. See [`docs/SECRETS_SETUP.md`](SECRETS_SETUP.md).

### Step 3: Verify the deployment

```bash
kubectl --context local-alpha get pods -n waddlebot
kubectl --context local-alpha rollout status deployment -n waddlebot --timeout=300s
```

Check the control-plane and community-facing services are healthy:

```bash
# hub-api (admin/tenancy/marketplace control plane)
kubectl --context local-alpha exec -n waddlebot deploy/waddlebot-hub-api-v3 -- \
  curl -sf http://localhost:8204/health

# hub-webui (admin portal)
curl -sf https://waddles.localhost.local/health
```

Expected response: `{"status":"healthy",...}`. If a pod is crash-looping, check its logs first —
`kubectl --context local-alpha logs -n waddlebot deploy/<name> --tail=100`.

### Step 4: Access the Admin Portal

- **Alpha:** `https://waddles.localhost.local`
- **Beta:** `https://waddlebot.penguintech.cloud`
- **Production:** `https://waddles.app`

For a fresh local/alpha install with no admin account yet, seed one with
`./scripts/seed-admin.sh --help` (creates a local-only default admin — **change the password
immediately after first login**; never carry the default into beta or production).

## First-Time Configuration

### 1. Create your first community

After logging in:

1. Navigate to **Communities** in the sidebar
2. Click **Create Community**
3. Fill in name, description, and defaults
4. Click **Create**

### 2. Connect a platform

1. Go to **Settings → Platforms** and choose Twitch, Discord, or Slack
2. Enter the platform credentials gathered above
3. Complete the OAuth flow
4. Select the channels/servers to monitor

### 3. Activate App Bundles

Waddles ships functionality as **App Bundles**, not fixed modules — a global admin installs a
bundle, a tenant admin makes it available, and a community admin activates it. From **Modules /
Marketplace** in the admin panel:

1. Confirm the bundles you need are **installed** (global admin) and **available** (tenant admin)
2. In your community's **Marketplace** tab, **activate** the bundles you want — e.g. AI chat,
   loyalty, Music Station, giveaways
3. Multiple bundles can be activated for the same feature at once (e.g. two giveaway variants) —
   they run side by side unless one declares the other `incompatible_with` it

See [Architecture — App Bundle model](ARCHITECTURE.md#app-bundle-model) for how installed →
available → activated works.

### 4. Configure commands

1. Go to **Commands** in the admin panel
2. View available commands by activated bundle
3. Customize aliases and per-command permissions

### 5. Set up OBS integration (optional)

1. Navigate to **Overlays** in the admin panel
2. Generate a browser-source URL for a surface: `full_screen`, `media`, `crawler`, or the Music
   Station player
3. In OBS: **Add Browser Source** → paste the URL → set dimensions (1920x1080 recommended)

## Helm Commands

```bash
# Upgrade after a values change
helm upgrade waddlebot ./k8s/helm/waddlebot -n waddlebot \
  --kube-context local-alpha -f k8s/helm/waddlebot/values-alpha.yaml

# View rendered manifests without applying
helm template waddlebot ./k8s/helm/waddlebot -f k8s/helm/waddlebot/values-alpha.yaml

# Uninstall
helm uninstall waddlebot -n waddlebot --kube-context local-alpha
```

## Troubleshooting

### Pod crash-looping

```bash
kubectl --context local-alpha logs -n waddlebot deploy/<name> --previous
kubectl --context local-alpha describe pod -n waddlebot <pod-name>
```

Common causes: missing secret, database not reachable yet (check the `db-migrate` init
container's logs), or a resource limit too low for local Kubernetes.

### Database connection errors

```bash
kubectl --context local-alpha logs -n waddlebot deploy/waddlebot-postgres
kubectl --context local-alpha get secret -n waddlebot waddlebot-db-credentials -o yaml
```

### Cannot access the admin portal

```bash
kubectl --context local-alpha get ingress -n waddlebot
kubectl --context local-alpha get pods -n waddlebot -l app.kubernetes.io/component=hub-webui-v3
```

Confirm your local DNS/hosts entry resolves `waddles.localhost.local` to the ingress controller's
address for local/alpha clusters.

## Security Considerations

1. **Change default credentials immediately** — admin portal password, any seeded dev accounts
2. **Secrets via Vault/Sealed Secrets/External Secrets Operator only** — never plaintext in a
   values file or committed `.env`
3. **TLS at ingress** — beta and production terminate TLS at the ingress controller
4. **Regular backups** — automate PostgreSQL backups; see [`docs/DATABASE.md`](DATABASE.md)
5. **Keep images current** — track Dependabot alerts and rebuild on security patches

## Next Steps

- **[Architecture](ARCHITECTURE.md)** — the 8-container pipeline and App Bundle model
- **[App Bundle SDK](plans/2026-08-31-app-bundle-sdk-design.md)** — author your own bundle
- **[Kubernetes](KUBERNETES.md)** — Helm chart reference
- **[Database](DATABASE.md)** — schema, migrations, per-service accounts
- **[Contributing](CONTRIBUTING.md)** — build and contribute new App Bundles

## Support

- **Documentation**: browse [`/docs`](.)
- **GitHub Issues**: report bugs at [github.com/penguintechinc/waddlebot/issues](https://github.com/penguintechinc/waddlebot/issues)
