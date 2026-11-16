# Claude Context Files

This directory holds local `.claude` context and symlinked standards references.

## Where Standards Live

- **Canonical rules**: `~/.claude/rules/*.md` (synced from `~/code/admin/rules/` — never edit standardized rules in individual repos).
- **App-specific context**: `CLAUDE.md`, `docs/APP_STANDARDS.md`, and `.claude/*.local.md` (such as `.claude/commands.local.md`).

## Stack Reference

- **Control-plane / API**: Python 3.13 + Quart + `penguin-dal` (`hub_api/`)
- **Data-plane / CLI / Executor**: Rust (`core/`) — Go is legacy-only (phased out for new services)
- **WebUI**: React 18 + Vite + TypeScript (`webui/`)
- **Kubernetes**: Helm v3/v4 (`k8s/helm/waddlebot/`)

## Usage

1. Read `CLAUDE.md` and `~/.claude/rules/` for project overview and cross-cutting rules.
2. Check `.claude/commands.local.md` and `docs/APP_STANDARDS.md` for Waddles-specific context.
