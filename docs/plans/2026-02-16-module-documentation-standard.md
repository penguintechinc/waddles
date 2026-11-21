# Module Documentation Standard — Full Compliance Plan

> **Historical document.** Written against the repository layout at the time; some paths named below (shown as plain text, not links) have since moved, been renamed or been removed. Not maintained as current architecture.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create the missing `docs/<module_name>/` folders (8-file standard) for every module that lacks one, and backfill `OVERVIEW.md` into the 11 existing compliant modules.

**Architecture:** Each module needs a `docs/<module_name>/` directory containing exactly these 8 Markdown files:
`OVERVIEW.md`, `USAGE.md`, `API.md`, `ARCHITECTURE.md`, `CONFIGURATION.md`, `TESTING.md`, `TROUBLESHOOTING.md`, `RELEASE_NOTES.md`.
Content is sourced by reading the actual module source code. Use an already-complete module (e.g. `docs/router_module/`) as a style and structure reference.

**Tech Stack:** Markdown, bash, existing module source code as ground truth.

---

## Background: Compliance Audit

### Partially Compliant (11 modules — 7 files each, missing OVERVIEW.md)

These modules have the original 7-file set but are missing `OVERVIEW.md`. **Phase 0** backfills it.

| docs/ folder | Source |
|---|---|
| `ai_interaction_module` | action/interactive/ai_interaction_module |
| `browser_source_core_module` | core/browser_source_core_module |
| `community_module` | core/community_module |
| `hub_module` | `admin/hub_module/` |
| `identity_core_module` | core/identity_core_module |
| `labels_core_module` | core/labels_core_module |
| `reputation_module` | core/reputation_module |
| `router_module` | processing/router_module |
| `security_core_module` | core/security_core_module |
| `unified_music_module` | core/unified_music_module |
| `workflow_core_module` | core/workflow_core_module |

### Incomplete Index Folders (not module docs — leave as-is)

- `docs/admin/` — admin section index, not a module (`hub_module` already covered above)
- `docs/interaction-modules/` — category index only
- `docs/core-modules/` — category index only

### Missing: 20 Module Documentation Folders

**Group A — Interactive Modules (9):** alias, calendar, inventory, loyalty, memories, quote, shoutout, spotify, youtube_music
**Group B — Action Modules (7):** discord, gcp_functions, lambda, openwhisk, slack, twitch, youtube
**Group C — Core Modules (4 active + 2 check-first):** ai_researcher, analytics, credential_manager, engagement, (module_rtc?), (video_proxy?)

---

## The 8-File Standard

Every module folder must contain these exact files. Before writing any file, **read the module's source code** (`ls` the module dir, read the main entrypoint, read the proto/API files, read the README if any).

| File | What it covers |
|---|---|
| `OVERVIEW.md` | **Entry point:** one-paragraph purpose, place in system, key capabilities, links to all other docs, maintainer, current version |
| `USAGE.md` | Getting started, running locally/Docker, health checks, common workflows |
| `API.md` | All endpoints or gRPC methods, request/response, parameters, error codes |
| `ARCHITECTURE.md` | Internal design, components, data flows, dependencies |
| `CONFIGURATION.md` | All env vars, setup options, feature flags, secrets |
| `TESTING.md` | How to run tests, mock data, test strategy, coverage targets |
| `TROUBLESHOOTING.md` | Common issues, debug commands, FAQ, solutions |
| `RELEASE_NOTES.md` | Version history: `## v0.1.0 — Initial documentation release` as starter entry |

**Reference implementation:** `docs/router_module/` — read any file there before writing to match structure and depth.

### OVERVIEW.md Template

```markdown
# <Module Name>

> **One sentence: what this module does and why it exists.**

## Purpose

[2-3 paragraphs: what problem it solves, where it fits in the architecture, which other modules interact with it.]

## Key Capabilities

- Capability 1
- Capability 2
- Capability 3

## Documentation Index

| Document | Description |
|---|---|
| [USAGE.md](USAGE.md) | Getting started, running locally, common workflows |
| [API.md](API.md) | Endpoints, request/response formats, error codes |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Internal design, data flows, component breakdown |
| [CONFIGURATION.md](CONFIGURATION.md) | Environment variables, setup, feature flags |
| [TESTING.md](TESTING.md) | Test strategy, mock data, how to run tests |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Common issues, debug steps, FAQ |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | Version history, migrations |

## Quick Reference

| Item | Value |
|---|---|
| Source | `path/to/module/` |
| Language | Python / Go / Node.js |
| Port | XXXX (gRPC) / YYYY (HTTP) |
| Maintained by | [Team/Person] |
```


---

## Phase 0: Backfill OVERVIEW.md into Existing 11 Modules

> These modules already have the 7-file set. They only need `OVERVIEW.md` added.
> Read the module's existing `ARCHITECTURE.md` and `USAGE.md` to source the content.

### Task 0: Add OVERVIEW.md to all 11 existing module doc folders

**Step 1: For each module below, read its existing docs**

Modules to update (in this order):
1. `docs/router_module/` — source: processing/router_module
2. `docs/ai_interaction_module/` — source: action/interactive/ai_interaction_module
3. `docs/browser_source_core_module/` — source: core/browser_source_core_module
4. `docs/community_module/` — source: core/community_module
5. `docs/hub_module/` — source: `admin/hub_module/`
6. `docs/identity_core_module/` — source: core/identity_core_module
7. `docs/labels_core_module/` — source: core/labels_core_module
8. `docs/reputation_module/` — source: core/reputation_module
9. `docs/security_core_module/` — source: core/security_core_module
10. `docs/unified_music_module/` — source: core/unified_music_module
11. `docs/workflow_core_module/` — source: core/workflow_core_module

For each: read `docs/<module>/ARCHITECTURE.md` (first 50 lines) and `docs/<module>/USAGE.md` (first 30 lines) to get the summary content.

**Step 2: Write OVERVIEW.md for each module**

Use the template above. Write accurate, module-specific content — not filler text.

**Step 3: Verify**
```bash
for m in router_module ai_interaction_module browser_source_core_module community_module hub_module identity_core_module labels_core_module reputation_module security_core_module unified_music_module workflow_core_module; do
  test -f "docs/$m/OVERVIEW.md" && echo "OK: $m" || echo "MISSING: $m"
done
```
Expected: 11 × "OK: <module>"

**Step 4: Commit**
```bash
git add docs/router_module/OVERVIEW.md docs/ai_interaction_module/OVERVIEW.md docs/browser_source_core_module/OVERVIEW.md docs/community_module/OVERVIEW.md docs/hub_module/OVERVIEW.md docs/identity_core_module/OVERVIEW.md docs/labels_core_module/OVERVIEW.md docs/reputation_module/OVERVIEW.md docs/security_core_module/OVERVIEW.md docs/unified_music_module/OVERVIEW.md docs/workflow_core_module/OVERVIEW.md
git commit -m "docs: backfill OVERVIEW.md to bring existing 11 modules to 8-file standard"
```

---

## Phase 1: Group A — Interactive Modules

### Task 1: `alias_interaction_module` docs

**Files to create:**
- `docs/alias_interaction_module/OVERVIEW.md`
- `docs/alias_interaction_module/USAGE.md`
- `docs/alias_interaction_module/API.md`
- `docs/alias_interaction_module/ARCHITECTURE.md`
- `docs/alias_interaction_module/CONFIGURATION.md`
- `docs/alias_interaction_module/TESTING.md`
- `docs/alias_interaction_module/TROUBLESHOOTING.md`
- `docs/alias_interaction_module/RELEASE_NOTES.md`

**Step 1: Read the source module**
```bash
ls action/interactive/alias_interaction_module/
```
Read: `action/interactive/alias_interaction_module/app.py` (or equivalent entrypoint)
Read: any `routes.py`, `models.py`, `validation_models.py`, `*.proto` files found.
Read: `docs/router_module/USAGE.md` as style reference.

**Step 2: Create the docs directory and all 8 files**
```bash
mkdir -p docs/alias_interaction_module
```
Write each file in turn. Base content on what you read from source. Use the router_module docs as depth/style reference. Use `## v0.1.0 — Initial documentation` as the sole RELEASE_NOTES entry.

**Step 3: Verify**
```bash
ls docs/alias_interaction_module/
# Expected: 8 files — USAGE.md API.md ARCHITECTURE.md CONFIGURATION.md TESTING.md TROUBLESHOOTING.md RELEASE_NOTES.md
```

**Step 4: Commit**
```bash
git add docs/alias_interaction_module/
git commit -m "docs: add module documentation standard for alias_interaction_module"
```

---

### Task 2: `calendar_interaction_module` docs

**Files to create:** `docs/calendar_interaction_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/interactive/calendar_interaction_module/
ls action/interactive/calendar_interaction_module/services/ 2>/dev/null
```
Read: `action/interactive/calendar_interaction_module/app.py`
Read: `action/interactive/calendar_interaction_module/validation_models.py`
Read: any `services/` files (availability_service.py, booking_service.py, calendar_oauth_service.py, group_availability_service.py)

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/calendar_interaction_module
```
The calendar module has OAuth, booking, and group availability — document these specifically in API.md and ARCHITECTURE.md.

**Step 3: Verify**
```bash
ls docs/calendar_interaction_module/
# Expected: 8 files
```

**Step 4: Commit**
```bash
git add docs/calendar_interaction_module/
git commit -m "docs: add module documentation standard for calendar_interaction_module"
```

---

### Task 3: `inventory_interaction_module` docs

**Files to create:** `docs/inventory_interaction_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/interactive/inventory_interaction_module/
```
Read: entrypoint and model files. Check if `docs/interaction-modules/inventory.md` exists — if so, read it for context to incorporate into the new structured docs.

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/inventory_interaction_module
```

**Step 3: Verify**
```bash
ls docs/inventory_interaction_module/
```

**Step 4: Commit**
```bash
git add docs/inventory_interaction_module/
git commit -m "docs: add module documentation standard for inventory_interaction_module"
```

---

### Task 4: `loyalty_interaction_module` docs

**Files to create:** `docs/loyalty_interaction_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/interactive/loyalty_interaction_module/
```
Read: app.py (or main entrypoint) and models.

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/loyalty_interaction_module
```

**Step 3: Verify**
```bash
ls docs/loyalty_interaction_module/
```

**Step 4: Commit**
```bash
git add docs/loyalty_interaction_module/
git commit -m "docs: add module documentation standard for loyalty_interaction_module"
```

---

### Task 5: `memories_interaction_module` docs

**Files to create:** `docs/memories_interaction_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/interactive/memories_interaction_module/
```

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/memories_interaction_module
```

**Step 3: Verify**
```bash
ls docs/memories_interaction_module/
```

**Step 4: Commit**
```bash
git add docs/memories_interaction_module/
git commit -m "docs: add module documentation standard for memories_interaction_module"
```

---

### Task 6: `quote_interaction_module` docs

**Files to create:** `docs/quote_interaction_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/interactive/quote_interaction_module/
```

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/quote_interaction_module
```

**Step 3: Verify**
```bash
ls docs/quote_interaction_module/
```

**Step 4: Commit**
```bash
git add docs/quote_interaction_module/
git commit -m "docs: add module documentation standard for quote_interaction_module"
```

---

### Task 7: `shoutout_interaction_module` docs

**Files to create:** `docs/shoutout_interaction_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/interactive/shoutout_interaction_module/
```

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/shoutout_interaction_module
```

**Step 3: Verify**
```bash
ls docs/shoutout_interaction_module/
```

**Step 4: Commit**
```bash
git add docs/shoutout_interaction_module/
git commit -m "docs: add module documentation standard for shoutout_interaction_module"
```

---

### Task 8: `spotify_interaction_module` docs

**Files to create:** `docs/spotify_interaction_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/interactive/spotify_interaction_module/
```
Note: Spotify module likely involves OAuth — document the OAuth flow in CONFIGURATION.md and ARCHITECTURE.md.

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/spotify_interaction_module
```

**Step 3: Verify**
```bash
ls docs/spotify_interaction_module/
```

**Step 4: Commit**
```bash
git add docs/spotify_interaction_module/
git commit -m "docs: add module documentation standard for spotify_interaction_module"
```

---

### Task 9: `youtube_music_interaction_module` docs

**Files to create:** `docs/youtube_music_interaction_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/interactive/youtube_music_interaction_module/
```
Note: YouTube Music involves OAuth and playlist management — document these specifically.

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/youtube_music_interaction_module
```

**Step 3: Verify**
```bash
ls docs/youtube_music_interaction_module/
```

**Step 4: Commit**
```bash
git add docs/youtube_music_interaction_module/
git commit -m "docs: add module documentation standard for youtube_music_interaction_module"
```

---

## Phase 2: Group B — Action Modules

> Action modules push content to external platforms. Source is at `action/pushing/<module_name>/`.
> API.md should document both: (a) the internal gRPC/REST API that calls this module, and (b) the external platform API it wraps.

### Task 10: `discord_action_module` docs

**Files to create:** `docs/discord_action_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/pushing/discord_action_module/
```
Read: main entrypoint, any proto files. Read `docs/router_module/API.md` to understand how action modules are called.

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/discord_action_module
```

**Step 3: Verify**
```bash
ls docs/discord_action_module/
```

**Step 4: Commit**
```bash
git add docs/discord_action_module/
git commit -m "docs: add module documentation standard for discord_action_module"
```

---

### Task 11: `gcp_functions_action_module` docs

**Files to create:** `docs/gcp_functions_action_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/pushing/gcp_functions_action_module/
```
Note: GCP Functions module involves Google Cloud credentials — document IAM requirements in CONFIGURATION.md.

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/gcp_functions_action_module
```

**Step 3: Verify**
```bash
ls docs/gcp_functions_action_module/
```

**Step 4: Commit**
```bash
git add docs/gcp_functions_action_module/
git commit -m "docs: add module documentation standard for gcp_functions_action_module"
```

---

### Task 12: `lambda_action_module` docs

**Files to create:** `docs/lambda_action_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/pushing/lambda_action_module/
```
Note: AWS Lambda module involves IAM credentials — document in CONFIGURATION.md.

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/lambda_action_module
```

**Step 3: Verify**
```bash
ls docs/lambda_action_module/
```

**Step 4: Commit**
```bash
git add docs/lambda_action_module/
git commit -m "docs: add module documentation standard for lambda_action_module"
```

---

### Task 13: `openwhisk_action_module` docs

**Files to create:** `docs/openwhisk_action_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/pushing/openwhisk_action_module/
```

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/openwhisk_action_module
```

**Step 3: Verify**
```bash
ls docs/openwhisk_action_module/
```

**Step 4: Commit**
```bash
git add docs/openwhisk_action_module/
git commit -m "docs: add module documentation standard for openwhisk_action_module"
```

---

### Task 14: `slack_action_module` docs

**Files to create:** `docs/slack_action_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/pushing/slack_action_module/
```
Note: Slack module involves bot tokens and workspace permissions — document in CONFIGURATION.md.

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/slack_action_module
```

**Step 3: Verify**
```bash
ls docs/slack_action_module/
```

**Step 4: Commit**
```bash
git add docs/slack_action_module/
git commit -m "docs: add module documentation standard for slack_action_module"
```

---

### Task 15: `twitch_action_module` docs

**Files to create:** `docs/twitch_action_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/pushing/twitch_action_module/
```
Note: Twitch module involves IRC and EventSub — document both channels in ARCHITECTURE.md.

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/twitch_action_module
```

**Step 3: Verify**
```bash
ls docs/twitch_action_module/
```

**Step 4: Commit**
```bash
git add docs/twitch_action_module/
git commit -m "docs: add module documentation standard for twitch_action_module"
```

---

### Task 16: `youtube_action_module` docs

**Files to create:** `docs/youtube_action_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls action/pushing/youtube_action_module/
```
Note: YouTube module involves OAuth and the YouTube Data API — document OAuth flow in CONFIGURATION.md.

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/youtube_action_module
```

**Step 3: Verify**
```bash
ls docs/youtube_action_module/
```

**Step 4: Commit**
```bash
git add docs/youtube_action_module/
git commit -m "docs: add module documentation standard for youtube_action_module"
```

---

## Phase 3: Group C — Core Modules

> Core modules are at `core/<module_name>/`. Check each module's status before documenting — two modules (module_rtc, video_proxy_module) had their k8s manifests deleted in this branch, so confirm they are still active before writing docs.

### Task 17: Confirm active status of `module_rtc` and `video_proxy_module`

**Step 1: Check if modules are still active**
```bash
ls core/module_rtc/ 2>/dev/null && echo "module_rtc: EXISTS" || echo "module_rtc: REMOVED"
ls core/video_proxy_module/ 2>/dev/null && echo "video_proxy: EXISTS" || echo "video_proxy: REMOVED"
```

**Step 2: Decision**
- If both EXIST with source files → add them to Tasks 21 and 22 below.
- If either is empty or removed → skip its docs; note the skip in a commit message comment.

**Step 3: No commit needed** — this is discovery only.

---

### Task 18: `ai_researcher_module` docs

**Files to create:** `docs/ai_researcher_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls core/ai_researcher_module/
```
Read: main entrypoint, any gRPC proto files, config files.

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/ai_researcher_module
```

**Step 3: Verify**
```bash
ls docs/ai_researcher_module/
```

**Step 4: Commit**
```bash
git add docs/ai_researcher_module/
git commit -m "docs: add module documentation standard for ai_researcher_module"
```

---

### Task 19: `analytics_core_module` docs

**Files to create:** `docs/analytics_core_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls core/analytics_core_module/
```

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/analytics_core_module
```

**Step 3: Verify**
```bash
ls docs/analytics_core_module/
```

**Step 4: Commit**
```bash
git add docs/analytics_core_module/
git commit -m "docs: add module documentation standard for analytics_core_module"
```

---

### Task 20: `credential_manager_module` docs

**Files to create:** `docs/credential_manager_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls core/credential_manager_module/
```
Note: This module likely handles secrets. SECURITY.md considerations apply — do NOT document actual secrets; document the env var names and vault integration patterns.

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/credential_manager_module
```

**Step 3: Verify**
```bash
ls docs/credential_manager_module/
```

**Step 4: Commit**
```bash
git add docs/credential_manager_module/
git commit -m "docs: add module documentation standard for credential_manager_module"
```

---

### Task 21: `engagement_module` docs

**Files to create:** `docs/engagement_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls core/engagement_module/
```

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/engagement_module
```

**Step 3: Verify**
```bash
ls docs/engagement_module/
```

**Step 4: Commit**
```bash
git add docs/engagement_module/
git commit -m "docs: add module documentation standard for engagement_module"
```

---

### Task 22: `module_rtc` docs (if active — see Task 17)

**Conditional:** Only execute if Task 17 confirmed this module is active.

**Files to create:** `docs/module_rtc/` — same 8 files.

**Step 1: Read the source module**
```bash
ls core/module_rtc/
```
Note: RTC module likely involves WebRTC or real-time comms — document signaling, ICE, and TURN server configuration in ARCHITECTURE.md and CONFIGURATION.md.

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/module_rtc
```

**Step 3: Verify**
```bash
ls docs/module_rtc/
```

**Step 4: Commit**
```bash
git add docs/module_rtc/
git commit -m "docs: add module documentation standard for module_rtc"
```

---

### Task 23: `video_proxy_module` docs (if active — see Task 17)

**Conditional:** Only execute if Task 17 confirmed this module is active.

**Files to create:** `docs/video_proxy_module/` — same 8 files.

**Step 1: Read the source module**
```bash
ls core/video_proxy_module/
```

**Step 2: Create docs directory and all 8 files**
```bash
mkdir -p docs/video_proxy_module
```

**Step 3: Verify**
```bash
ls docs/video_proxy_module/
```

**Step 4: Commit**
```bash
git add docs/video_proxy_module/
git commit -m "docs: add module documentation standard for video_proxy_module"
```

---

## Phase 4: Final Compliance Check

### Task 24: Verify full compliance

**Step 1: Run compliance check**
```bash
# List all module doc folders and their file counts
for dir in docs/*/; do
  count=$(ls "$dir"*.md 2>/dev/null | wc -l)
  echo "$count files: $dir"
done
```

**Step 2: Identify any folder with fewer than 7 .md files**
Any folder showing < 8 files is non-compliant and needs the missing files added.

**Step 3: Check existing compliant modules haven't regressed**
```bash
for module in docs/router_module docs/ai_interaction_module docs/browser_source_core_module; do
  echo "=== $module ==="
  ls "$module/"
done
```

**Step 4: Final commit**
```bash
git add -p  # review any remaining unstaged changes
git commit -m "docs: complete module documentation standard compliance audit"
```

---

## Notes on "translate" Module

The user noted `docs/translate` as missing. During codebase exploration, no standalone `translate_*_module` directory was found in action/interactive, `core/`, or action/pushing. Translation functionality exists as a service within processing/router_module/services/translation_providers.

**If a translate module is planned or already exists elsewhere:**
- Follow the same Task pattern above: read source → create `docs/translate_interaction_module/` with 7 files → commit.
- If it's a new module being developed, create placeholder docs with a `## v0.1.0 — Planned` entry in RELEASE_NOTES.md.

---

## Summary

| Phase | Action | Tasks |
|---|---|---|
| Phase 0 — Backfill OVERVIEW.md | 11 existing modules, +1 file each | Task 0 |
| Phase 1 — Interactive | 9 new module doc folders | Tasks 1–9 |
| Phase 2 — Action | 7 new module doc folders | Tasks 10–16 |
| Phase 3 — Core | 4–6 new (2 conditional) | Tasks 17–23 |
| Phase 4 — Audit | Full compliance check | Task 24 |
| **Total** | **31–33 modules touched** | **25 tasks** |

**Estimated output:**
- 11 backfilled `OVERVIEW.md` files (Phase 0)
- 20–22 new `docs/<module>/` folders × 8 files = **160–176 new Markdown files**
- **Grand total: ~187 files created/modified**
