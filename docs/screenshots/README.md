# Waddles Screenshots

This directory contains screenshots of the Waddles admin interface and features.

## Capturing Screenshots

`make screenshots` regenerates the whole set (unauthenticated + authenticated)
with a pinned Playwright (`tests/screenshots/`, playwright 1.63.0, lockfile
committed). Never hand-update a subset.

```bash
# 1. Running hub-webui + database (Kubernetes/Helm local alpha; no Docker Compose)
# 2. Seed: admin login account + UI content (communities 9001-9004, members,
#    leaderboard, overlays, chat, bundle activations from app_catalog)
export POSTGRES_PASSWORD=...  ADMIN_EMAIL=...  ADMIN_PASSWORD=...
make seed-mock-data            # SEED_ARGS=--docker to exec into a postgres container
# 3. Capture
export BASE_URL=http://localhost:8060        # default; point at any hub-webui
export SCREENSHOT_EMAIL="$ADMIN_EMAIL" SCREENSHOT_PASSWORD="$ADMIN_PASSWORD"
make screenshots
```

| Env | Default | Purpose |
|---|---|---|
| `BASE_URL` | `http://localhost:8060` | hub-webui to capture |
| `SCREENSHOT_EMAIL` / `SCREENSHOT_PASSWORD` | `ADMIN_EMAIL` / `ADMIN_PASSWORD` | login (never hardcoded) |
| `COMMUNITY_ID` / `TENANT_SLUG` | `9001` / `default` | seeded community and tenant |
| `OUT_DIR` | `docs/screenshots` | output (files overwritten by page name) |
| `STRICT_EMPTY=1` | off | fail on suspected empty-state pages (default: warn) |

- Fails (non-zero) on login failure, redirect to `/login`, HTTP >= 400, rendered
  error text, or zero captures; prints counts examined.
- Playwright scratch `/tmp/playwright-waddles` is removed on exit, pass or fail.
- Page inventory: `tests/screenshots/pages.cjs`. After capture, review every image
  per the `capturing-marketing-screenshots` skill (error banners, empty states).
- Do not run against shared alpha; use a local or CI stack.

## Screenshots Needed

- [ ] `login.png` - Login page
- [ ] `dashboard.png` - Main dashboard
- [ ] `communities.png` - Communities list
- [ ] `community-dashboard.png` - Individual community dashboard
- [ ] `servers.png` - Server/channel configuration
- [ ] `routes.png` - Command routes
- [ ] `modules.png` - Module registry
- [ ] `users.png` - User management
- [ ] `settings.png` - Account settings

## Adding Screenshots to README

Once screenshots are captured, update the main README.md to include them:

```markdown
## Screenshots

### Dashboard
![Dashboard](docs/screenshots/dashboard.png)

### Community Management
![Communities](docs/screenshots/communities.png)

### Module Registry
![Modules](docs/screenshots/modules.png)
```
