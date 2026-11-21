# Canonical Module Sources (Consolidated Services)

> **Status (v3.0.X, Task 0.1):** The 21 orphaned `services/*/<module>/` duplicate
> subdirectories this file used to track have been **deleted**. Only the canonical
> trees remain. The `services/*` **aggregators** (`app.py` / `config.py` /
> `requirements.txt`) are kept for now — they are the working prototype for the
> seven-container consolidation in Task 0.5 — and `services/` is removed entirely at
> P5 once parity is confirmed.

## Canonical trees

Module code lives in exactly one place. Edit these:

| Kind | Canonical tree |
|------|----------------|
| Interactive actions | `action/interactive/<module>/` |
| Pushing actions | `action/pushing/<module>/` |
| Core services | `core/<module>/` |
| Trigger receivers | `trigger/receiver/<module>/` |
| Router / processing | `processing/<module>/` |

The consolidated `services/*` containers COPY module code **from** these trees at build
time (e.g. services/interactive-social/Dockerfile does
`COPY action/interactive/alias_interaction_module ./alias_interaction_module`). The
aggregator's own `app.py`/`config.py`/`requirements.txt` are the only files under
`services/*` that are real build inputs.

## Why the orphans were deleted, not ported

The duplicates were shadowed at build time — every `services/*/Dockerfile` overwrote its
local `<module>/` with a `COPY` from a canonical tree — so nothing built from them. Before
deleting, all 21 were diffed against their canonical copies (Task 0.1, Step 2). The plan's
hypothesis was that they carried *newer* component-based DB/Redis config worth porting back.
The analysis found the reverse: **the orphans are the older side.**

- Most `config.py` diffs are regressions — `postgresql://` reverted to the deprecated
  `postgres://`, older dependency pins (aiohttp, protobuf, pytest), or a stray incomplete
  `pydal → penguin_dal` import that nothing else in the tree supports.
- The services/core-identity/identity_core_module orphan was **security-regressed** —
  it dropped the per-user authorization guard in `grpc_handler.py` (an IDOR: any valid
  token could enumerate any user's linked platforms) and hardcoded a fallback
  `SECRET_KEY`. Deleting it removes a hazard rather than losing work.
- Where the orphans *did* carry a component-based DB/Redis `config.py` (interactive-social,
  trigger-webhooks), it was not worth salvaging: the DB half is redundant under Helm (the
  supported deploy path assembles `DATABASE_URL` in `secrets.yaml`), and the orphans mixed
  in stale `localhost`/`router-service` defaults.

## Finding surfaced by the diff (separate from this deletion)

Helm provides Redis as **components** (`REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` /
`REDIS_*_PASSWORD`) but never assembles a `REDIS_URL`, while **44 canonical modules read
`REDIS_URL`** (defaulting to `redis://localhost:6379/0`) and only 6 read `REDIS_HOST`. Under
the Helm-only deploy path that is a latent fleet-wide mismatch: those modules would fall back
to localhost for Redis. The fix is a deliberate, fleet-wide change — either assemble
`REDIS_URL` in the Helm secret (mirroring `DATABASE_URL`) or move the modules to the
component pattern — not a salvage of four stale orphan configs. Tracked as follow-up, out of
Task 0.1's scope.
