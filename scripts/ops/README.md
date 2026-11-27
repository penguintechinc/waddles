# scripts/ops

Operator scripts with a `make` target each. Every script supports `--help`, runs under
`set -euo pipefail`, prints counts and fails on zero items examined. Self-tests run under
`make test-scripts` (`gh` is stubbed via `PATH`, no network).

## Merge and branch hygiene

| Make target | Script | What it does |
|---|---|---|
| `make release-merge PR=<n> SHA=<sha> [MAX=<min>] [DRY=1]` | `scripts/release-merge.sh` | Waits for every check (ignores `submit-queue`) plus `ci-gate`, then merges into `release/*` pinned to the 40-hex head (`--match-head-commit`). Verifies the merge commit landed on the base. Never `--admin`, never main. |
| `make merge-when-green PR=<n> SHA=<sha> [DRY=1]` | `scripts/release-auto-merge.sh --exit-on-stuck` | Single-PR watcher: reruns infra flakes once per run (apt-mirror timeouts, `sccache: error: Timed out waiting for server startup`) and a stale `ci-gate`, then calls `release-merge.sh`. Exits 1 on a real failure, rerun cap, zero checks or head drift. |
| `make release-auto-merge PRS="<n> ..." [DRY=1]` | `scripts/release-auto-merge.sh` | Same watcher over an ordered PR list (stacked PRs wait for their parent). |
| `make merge-release-into-prs PRS="<n> ..." [DRY=1]` | `merge-release-into-prs.sh` | Merges `origin/<release base>` into each open PR head in a throwaway worktree: signed merge commit and plain push; conflicts aborted and reported. `COMMIT_TRAILERS` adds attribution lines. |
| `make cleanup-merged-pr PRS="<n> ..." [DRY=1]` | `cleanup-merged-pr.sh` | For MERGED release PRs: removes clean worktrees, then the remote and local branch, only when contained in `origin/<base>`. |
| `make prune-worktrees [ARGS=--apply]` | `prune-agent-worktrees.sh` | Sweeps all agent worktrees under `.claude/worktrees/` and `.worktrees/`, keeping anything dirty, locked, unpushed or with an open PR. |

Repo comes from `REPO` or `gh repo view`; the base comes from each PR's `baseRefName` and must be `release/*`.

## Other ops

| Make target | Script |
|---|---|
| `make platform-secrets-rotate ARGS=...` | `platform_secrets_rotate.py` |
| `make command-latency-report` | `command_latency_report.py` |
| `make seed-latency-canary ...` | `seed_latency_canary.py` |
