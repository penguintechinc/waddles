# Claude Code Context

**Repo-specific context for Claude Code in this repo.** Holds repo rules, MCP notes and setup pointers. Standard rules and skills are provided globally (see below).

## Repo-Owned Files

**These files are owned by this repo and edited directly via PR:**

- ✅ **EDIT DIRECTLY** `CLAUDE.md`, `.claude/*.md`, `docs/STANDARDS.md`, `docs/standards/*.md`
- ✅ **CREATE NEW FILES** for app-specific context:
  - `docs/APP_STANDARDS.md` - App-specific architecture, requirements, context
  - `.claude/{subject}.local.md` - Project-specific overrides (e.g., `architecture.local.md`, `python.local.md`)

**App-Specific Addendums to Standardized Files:**

If your app needs to add exceptions, clarifications, or context to standardized `.claude/` files (e.g., `react.md`, `python.md`, `testing.md`), either edit those files directly via PR or create a `.local` variant:

- `react.md` (standardized) → Create `react.local.md` for app-specific React patterns
- `python.md` (standardized) → Create `python.local.md` for app-specific Python decisions
- `testing.md` (standardized) → Create `testing.local.md` for app-specific test requirements
- `security.md` (standardized) → Create `security.local.md` for app-specific security rules

**Local Repository Overrides:**

This repository may contain `.local.md` variant files that provide project-specific overrides or addendums:
- `.claude/*.local.md` - Project-specific overrides to standardized `.claude/` rules

**Always check for and read `.local.md` files** alongside standard files to ensure you have the complete context for this specific repository.

## Global Rules and Skills

**Standard rules and skills are provided globally at `~/.claude/{rules,skills}/`** (source: `~/code/admin/rules`). They are NOT symlinked into individual repos.

- **Global** (`~/.claude/rules/*.md`, `~/.claude/skills/*/SKILL.md`): Apply to all projects

---

## MCP Servers

- **mem0**: Persistent memory across sessions. At the start of each session, `search_memories` for relevant context before asking the user to re-explain anything. Use `add_memory` whenever you discover project architecture, coding conventions, debugging insights, key decisions, or user preferences. Use `update_memory` when prior context changes. Save information like: "This project uses PostgreSQL with Prisma", "Tests run with pytest -v", "Auth uses JWT validated in middleware". When in doubt, save it, future sessions benefit from over-remembering.

---

## Setup Script

Environment setup is provided by `setup.sh` in the `admin` repo (`~/code/admin/.claude/setup.sh`), not shipped in this repo. It configures the local Claude Code environment:

```bash
~/code/admin/.claude/setup.sh              # Full setup (statusline + mem0 + settings)
~/code/admin/.claude/setup.sh statusline   # Statusline only
~/code/admin/.claude/setup.sh mem0         # mem0 + Qdrant only
~/code/admin/.claude/setup.sh settings     # Settings update only
```

At session start, verify the environment is configured. If `~/.claude/statusline-command.sh` or `~/.claude/mcp/mem0/mcp-server.py` does not exist, run `~/code/admin/.claude/setup.sh`.

### Status Line

The setup script symlinks `statusline-command.sh` to `~/.claude/` and configures `settings.json`. The statusline displays model, effort, repo, branch, context usage, cost, and duration.

### mem0 (Local Persistent Memory)

The setup script deploys a local Qdrant container for vector storage and configures a mem0 MCP server using Ollama for embeddings (`nomic-embed-text:latest`, 768 dims) and LLM (default `gemma4:e4b`). Ollama host selection is remote-first via `OLLAMA_BASE_URL`, with a local fallback; `gemma4:e2b` is used only on weak hosts. Selection logic: `~/.claude/mcp/mem0/ollama_select.py`. No third-party API calls.

**Manage Qdrant:**
```bash
docker compose -f ~/.claude/mcp/mem0/docker-compose.yml up -d    # start
docker compose -f ~/.claude/mcp/mem0/docker-compose.yml down      # stop
```

**Qdrant dashboard:** http://localhost:6333/dashboard
