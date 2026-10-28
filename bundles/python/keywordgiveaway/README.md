# keywordgiveaway (Python)

`!kgiveaway` -- a per-community **keyword giveaway**: a mod opens it with a keyword, viewers enter
by typing the keyword (or `!kgiveaway enter`), a mod closes it, draws a random winner and can
reroll. First-party (`provider: builtin`, `app_id: waddles.core.example.keywordgiveaway`),
kv-only, WASI component. Original Waddles content.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped (state record + slotted, pseudonymous entrants) |
| Catalog | NOT yet in `bundles/core-bundles.yaml` (added at registration time) |

## Not the same as `giveaway` / `raffle`

| | `keywordgiveaway` (this bundle) | `giveaway` (PR #853) | `raffle` |
|---|---|---|---|
| Entry | type the **mod's keyword** (any chat line equal to it) or `!kgiveaway enter` | `!enter` / `!giveaway enter` | `!raffle` / `!enter` |
| Lifecycle | mod `open <keyword>` / `close`, no clock | timed (`start 5m`) | mod `open`/`close` |
| Winners | `draw` + any number of `reroll`s (never the same entrant twice) | one, then it closes | one per `draw` |
| Trigger | `!kgiveaway` | `!giveaway`, `!enter` | `!raffle`, `!enter` |

Distinct name **and** trigger on purpose: all three can be activated in one community without
answering each other's commands (`giveaway` and `raffle` already share `!enter`).

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!kgiveaway open <keyword> [prize]` | mod/broadcaster | Starts a NEW giveaway. Keyword: 2-24 of `a-z 0-9 _`, optional leading `!`, case-insensitive, never `!enter` / `!raffle` / `!giveaway` / `!kgiveaway`. Prize: free text up to 80 chars. Refused while one is open. |
| `<keyword>` (the whole chat line) | anyone | Enters, **silently** -- no reply on success or on a duplicate (a big chat would flood the bot's outbound rate limit). |
| `!kgiveaway enter` | anyone | Explicit entry; always answers: entered (`entry #N`) / already entered / none open / closed / full (2000). |
| `!kgiveaway close` | mod/broadcaster | Stops entries, keeps them for the draw. |
| `!kgiveaway draw` | mod/broadcaster | Closes the pool and announces one winner as a `{user:<token>}` mention. Refused if already drawn (use `reroll`) or nobody entered (the giveaway stays open). |
| `!kgiveaway reroll` | mod/broadcaster | Another winner from the entrants who have not won. Refused before a first `draw` and once everyone has won. |
| `!kgiveaway` / `status` / `list` | anyone | State, keyword, entry count (+ prize / winners drawn). |
| anything else | anyone | Usage reply -- never silently dropped. |

The mod gate is strict-boolean and **fail-closed**: only a real `is_mod is True` /
`is_broadcaster is True` admits; absent badges (Discord's normalizer today), strings and ints
deny, before any `kv` access. `open` carries free text, so the domain verbs (`open`/`close`/
`draw`/`reroll`/`enter`/`status`) are special-cased ahead of `waddle_sdk.command.parse_command`
(they are not in the shared `VERBS`), exactly like `raffle`; the bare form and `list` go through the
real grammar.

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | the giveaway state record and the slotted entrants |
| `chat.send:discord` / `chat.send:twitch` | replies through the relay host import |

No `db`, no `http`/egress, no `flags.read` (activation is the only gate), no AI.

## Entrants and winners

- **One entry per viewer, atomically.** `kv.increment` is the only compare-and-swap: the
  caller that sees `1` on the per-(round, pseudonym) marker owns the entry, any later one sees
  `> 1`. A second `increment` hands out the slot number; the entry record is written to
  `kwgiveaway.e.<rid>.<slot>`. Fifty interleaved entrants get fifty distinct slots (tested against the fake's atomic `increment`).
- **Compensation, not corruption.** If a later write fails, the marker is released so the
  viewer can simply re-enter; a slot whose record never landed (or expired, or is unparseable) is a
  *hole* the draw skips. A draw spends at most 40 entry reads (host budget: 64 kv ops).
- **Winner rendering.** At entry the bundle keeps the host-minted `{user:<token>}` mention
  placeholder (a pseudonym, never a name) and 8 hex of the pseudonym. The winner is announced
  through that placeholder, which the egress detokenizer resolves to a display name. The token is
  re-validated against the grammar when read back, so a tampered kv value can never inject a
  second placeholder. An entrant whose actor was a raw name is announced as `entrant <8 hex>`.
- **Randomness.** `random.SystemRandom().randrange(pool)` (OS entropy). Never the module-level
  `random` functions (componentize-py freezes their import-time state into the build) and never a
  hash of public inputs such as the clock, which a moderator choosing the moment to draw could
  steer.

## State

| Key | TTL | Value |
|---|---|---|
| `kwgiveaway.rid` | 30 d | round counter (`increment`) |
| `kwgiveaway.state` | 3 d | JSON `{rid, kw, prize, open, drawn:[slot...]}` |
| `kwgiveaway.m.<rid>.<pseudonym32>` | 3 d | one-entry marker (`increment`) |
| `kwgiveaway.n.<rid>` | 3 d | slot counter (`increment`) |
| `kwgiveaway.e.<rid>.<slot>` | 3 d | JSON `{t: token \| null, p: 8 hex}` |

Colon-free keys (the host reserves `:`), prefixed `c.<community>.` by `community_kv`. 2 keys per
entrant x 2000 max stays well inside the host's 10,000-key quota. An abandoned giveaway expires
on its own after 3 days. `community=None` is the tenant-wide sentinel (scope `0`); an empty id
fails loud.

## Safety

- **PII-free.** Keys hold only SHA-256 of `(platform, author_id or actor)`; logs carry the op
  and counts (never a keyword, prize, actor or message text).
- **Echoed text is sanitized.** The keyword and prize go through
  `waddle_sdk.safe_text.sanitize_user_text` on every echo (`@everyone`, `<@id>`, forged
  `{user:..}` placeholders, control characters). The invoker's label keeps its own host
  placeholder live via `allow_tokens` and sanitizes any raw name.
- **A token is only ever the host-minted one (security review).** The winner label is built as
  `{user:<token>}`, so the token must be exactly what the pipeline minted: it is taken from
  `event.actor` only when the WHOLE actor is one placeholder (`fullmatch`, ASCII token grammar,
  <= 256 chars -- a padded, embedded, doubled, escaped or look-alike actor is not one), stored
  as such, and validated again against the same grammar when read back. Tests check this against
  the SDK's port of the egress detokenizer (`safe_text._split_live`): in every reply the live
  placeholders are exactly the expected token, never more. A raw actor is sanitized and announced
  as `entrant <8 hex>`.
- **No reply can lead with a chat command.** Replies that begin with the entrant's label pass
  through `guard_leading_command`, so a raw handle starting `/`, `.` or `!` is prefixed with a
  speech-bubble instead of running as the bot's own command.
- **Choosing a keyword.** `open` refuses the sibling giveaway bundles' own commands (`!enter`,
  `!raffle`, `!giveaway`, `!kgiveaway`): typing one would enter this giveaway AND the other bundle.
  Any other bundle's command used as a keyword is the moderator's choice (a bare word such as `win`
  cannot collide with anything).
- **Fail loud.** A `kv` error is logged (`op` + exception class only) and answered in chat on the
  command path; on the keyword path it raises (a swallowed error would drop entries silently).
  A corrupt state record is answered/raised the same way and a mod's `open` is the recovery.
- **Cost.** The stage sees every chat line (no `filters.command_prefix`, like `greeter`) -- lines
  longer than a keyword return before any `kv` read; a short line costs one read of the state
  record.

## Known limits

- Replayed `reroll`/`draw` events are not de-duplicated (entry is idempotent via the marker; a
  replayed `reroll` would pick a further winner).
- No timer: use `giveaway` for timed draws. No weighted/multiple entries, no eligibility (sub/
  follower) rules, no DM to the winner.

## Test

```bash
uv venv -p 3.13 .venv   # at the repo/worktree root, never under bundles/
uv pip install --python .venv/bin/python3 -e sdk/waddle-sdk pytest pytest-cov mypy ruff
cd bundles/python/keywordgiveaway
../../../.venv/bin/python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
The suite uses `waddle_sdk.testing.FakeKvHost` (real key validation + atomic `increment`) behind a
failure-injection wrapper -- there is no structured-`db` table, so no `wit_fake_db`.

This bundle imports `waddle_sdk.safe_text`, which lives on `feature/sdk-output-sanitizer`
(merged into this branch); the sanitizer PR must land before this one.
