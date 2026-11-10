# waddles.core.service.ai

Always-on `kind: service` bundle (Rust, `world stage-tools`) exposing guarded AI tools to app
bundles (AI Teammates, DJ, ...) through `tool.invoke`. It wraps the host `ai.generate`
capability: **model choice (default `gemma4:e2b`, `e4b` for structured/JSON), BYOK/tier gating,
PII rejection and prompt framing are all host-side.** The bundle holds no API key, model name
or provider URL, and requests only `ai.generate` + `storage.kv`.

## Tool contract (authoritative: `tests/contract/*.json`, mirrored in both manifests)

All args objects reject unknown fields. Errors are typed `invalid-args` codes (below).

| Tool | Access / risk | Input | Output |
|---|---|---|---|
| `ai.complete` | read / normal | `prompt` (1..3500 B), `system?` (<=1200 B), `max_tokens?` (1..512), `temperature?` (0..2) | `{text}` (<=2000 chars) |
| `ai.classify` | read / normal | `text` (1..2000 B), `labels` (2..20 unique, each 1..40 B of `[A-Za-z0-9_.- ]`), `instructions?` (<=800 B) | `{label, confidence 0..1, reason<=200}`; `label` is always one of `labels` |
| `ai.rag.query` | read / normal | `collection` (`^[a-z0-9][a-z0-9_.-]{0,39}$`), `question` (1..1000 B), `top_k?` (1..8) | `{answer, citations:[{doc_id, chunk, score?}]}` |
| `ai.rag.ingest` | write / dangerous, `required_scope: community:admin` | `collection`, `doc_id?`, `title?`, `text` (1..100000 B) | `{doc_id, chunks}` |

**`ai.rag.*` currently return `not_available:rag`** after full arg validation (and the admin-scope
check for ingest): the host exposes no RAG/memory capability to bundles yet. They never call the
model and never consume quota. See "Needs from S0".

Note: the catalog table lists `ai.rag.{ingest,search,delete}`; this bundle ships
`ai.rag.query`/`ai.rag.ingest` per the C3 task. The catalog and this contract must be reconciled
before the host RAG seam lands.

## Error codes (`invalid-args` messages; never contain prompt/response text)

`invalid_args`, `*_length`, `max_tokens_out_of_range`, `temperature_out_of_range`, `labels_count`,
`labels_duplicate`, `label_shape`, `collection_shape`, `doc_id_shape`, `top_k_out_of_range`,
`prompt_too_large`, `input_rejected` (blocklist), `content_filtered`, `quota_exceeded:minute|daily`,
`invalid_model_output`, `invalid_config`, `denied:<code>` (e.g. `denied:contains_pii`),
`too_many_tokens`, `backend`, `not_available:rag`. Typed WIT errors: `not-granted` (caller or
`ai.generate` not granted), `forbidden` (ingest without admin scope), `target-unavailable` (host AI
off/unreachable, kv down -- quotas fail closed).

## Prompt-injection guard

* The host API takes one prompt string, so the bundle lays out a trusted `<<WADDLE_RULES>>`
  block (caller `system` + a fixed isolation notice), an optional `<<WADDLE_GUIDANCE>>` block
  (`ai.classify` instructions) and the untrusted `<<WADDLE_DATA>>` block.
* Every `<`/`>` in caller text becomes a look-alike (`‹ ›`) and invisible/control/bidi characters
  are stripped, so text can neither forge nor close a marker (this also defangs chat-template
  tokens). The host additionally frames the whole prompt as untrusted.
* Callers pass chat text only as `prompt`/`text`, never inside `system`.
* Output: control/zero-width stripped, links replaced by `[link removed]` (unless the community
  sets `allow_links`), `@everyone`/`@here` neutralized, marker leaks refused, hard cap 2000 chars;
  classification output must parse to a label in the supplied set or the call fails loud.
* Moderation hook (`Moderation` trait, shipped impl = blocklist) runs on input and output.
* No tools are ever exposed to the model; completions are never executed.

## Per-community config (bundle kv key `config`, JSON; absent = defaults, invalid = loud)

`{"rpm": 10, "daily": 1000, "blocklist": ["term", ...], "allow_links": false}` with
`rpm` 1..60, `daily` 1..100000, blocklist <=100 terms (<=64 B, case-insensitive substring).
Quotas are fixed-window kv counters (`q.m.<minute>`, `q.d.<day>`), community-scoped by the host.
The host additionally enforces `ai.generate`'s own 10 calls / 60 s.

## Needs from S0 (host seams)

1. A RAG/memory host capability for bundles (ingest/chunk/embed/search with per-community
   collections, citations, admin-gated writes) -- unblocks `ai.rag.*`.
2. A separate `system` field (or role-split prompt) on `ai.generate`; today the bundle must fold
   system text into the single prompt with delimiters.
3. A host-side moderation seam (bundle-callable) to complement the in-bundle blocklist.

## Logs

One DEBUG line per call: `{tool, outcome, out_bytes}` -- closed vocabulary, no ids beyond the
host's community context, never args or text.

## Build / test

```
cargo component build --release --target wasm32-wasip2 --locked   # also done in bundles/Dockerfile.core-bundles
cargo test --locked && cargo clippy --all-targets --locked -- -D warnings && cargo deny check
python3 -m pytest tests/test_manifest_contract.py
```
