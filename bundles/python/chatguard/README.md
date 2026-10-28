# chatguard (Python)

Configurable spam / content filters that delete the offending message (Discord) and warn, plus a
`permit` command for links. First-party (`provider: builtin`,
`app_id: waddles.core.example.chatguard`).

| Filter | Fires when | Default |
|---|---|---|
| `blacklist` | a blacklisted word/phrase appears (whole-word, case/accent/zero-width-insensitive) | off |
| `links` | a URL, bare domain on a common TLD or IPv4 literal appears (`[.]`, `(dot)`, ideographic stops, zero-width chars are normalized) | off |
| `caps` | >= `caps` % of >= `capsmin` letters are capitals (mentions/URLs/custom emotes ignored) | off, 70 % / 10 |
| `symbols` | >= `symbols` % of >= `symbolsmin` visible characters are punctuation/symbols/emoji (a Discord custom emote counts as one) | off, 60 % / 10 |

Broadcasters and moderators (`is_mod is True` / `is_broadcaster is True`) are exempt from every
filter and are never read from kv for it; a string/integer badge is NOT a moderator.

| Command (moderators/broadcasters only) | Behavior |
|---|---|
| `!chatguard enable\|disable <blacklist\|links\|caps\|symbols>` | Toggle one filter (`!chatguard caps enable` also works). |
| `!chatguard set caps\|capsmin\|symbols\|symbolsmin\|permit\|warn <n>` | Thresholds, default permit length, warning cooldown (0 = warn every time). |
| `!chatguard add\|remove blacklist <word or phrase>` | Up to 50 entries; entries are never echoed or listed. |
| `!chatguard list` / `reset` | Summary (blacklist as a count only) / restore defaults. |
| `!permit <@user> [seconds]` (alias `!chatguard permit ...`) | That user may post links for N seconds (default 60, max 600). |

`!permit` is not a `parse_command` shape (its first argument is a mention) so it has its own small
parser; everything else uses `waddle_sdk.command.parse_command`. Grammar errors reply a static usage
line and never echo the input.

## Moderation capability -- what the host actually has

Verified against the host, not assumed:

| Capability | Status |
|---|---|
| `chat.delete:discord` | **Used.** The host confirms the delete with the platform before `relay.push` returns; it only ever targets the triggering event's own channel (`core/svc_action/src/capabilities.rs` `relay_op_supported`, `handle_discord_queued_op`). Declared in both manifests, least-privilege and justified. |
| `chat.delete:twitch` | **Not requested.** Authorized-then-refused (`unsupported_op`); the Helix client is still a STUB at `core/svc_ingest/src/outbound_ops.rs:377`. On Twitch chatguard only **warns** (the warning does not claim a removal). `DELETE_PLATFORMS` and the manifest permission are the two switches to flip when it lands. |
| timeout / ban | **Does not exist -- gap, not invented.** The relay ops are exactly `chat.send` / `chat.delete` / `dm.send` (`RelayOp`); the gate catalog's `moderation.<platform>` family has no WIT import (`wit/waddle-bundle/stage.wit` lists moderation as future `stage-next` work) and no host handler. **Follow-up: a timeout host capability** (relay op + permission + Twitch Helix `POST /moderation/bans`, Discord member timeout). `tests/test_manifest.py` pins all three facts and FAILS (never skips) when the host changes. |

## Where it runs

`identity` is served by the process stage only, `relay` by the action stage only (verified in
`core/svc_process` / `core/svc_action`), so `transform` decides -- config (1 kv read), permit,
rule evaluation, offender pseudonym -- and emits a `violation` event carrying **only** the rule
name, the platform message id, a pseudonym and the warn cooldown; `dispatch` enforces. The tests
model the stage split and fail on a capability used from the wrong stage.

The manifest consumes every `chat.message` (no `command_prefix` filter, like `greeter`); runtime
filter matching is the bundle's own, so mods and unconfigured communities cost one kv read or none.

## Enforcement and failure handling

1. Delete (Discord, snowflake-validated message id). 2. Warning, at most once per `warn` seconds
per user (`kv.increment` CAS on a TTL'd key; the pseudonym comes from `identity.resolve_actor`,
unlinked authors share one anonymous bucket). 3. If the delete failed the warning is still sent
(worded "heads up", no claim of removal) and `dispatch` then raises, so the failure is visible in
the stream metrics instead of swallowed; a redelivery retries the delete but the cooldown stops a
duplicate warning.

No per-event dedupe marker is minted on purpose: a spammer would fill the 10,000-key kv quota
with markers and the bundle would fail closed exactly when it is needed. Only the per-user warn
keys (TTL = cooldown) and permits (TTL = permit, mod-issued) exist besides the `chatguard.config`
blob. Fail-closed: an unlinked/unresolvable author can never hold a permit; an identity outage
enforces the link rule and logs at ERROR.

A corrupt config blob is logged at ERROR and treated as "no rules" (a poisoned value must not make
every chat line a transform failure); the next moderator command overwrites it. A kv failure while
screening a line raises (never a silent pass). Config edits are read-modify-write (moderator-only,
last write wins).

## Privacy

**Logs never contain the offending message, the author, a handle or a message id** -- only
`op` / `rule` / `action` / `platform` / `community_id` / error-class fields (asserted by
`test_the_offending_message_never_reaches_logs_keys_or_relays` and
`test_logs_only_carry_the_documented_fields`). The `violation` event has no `text`. kv keys are
`chatguard.config`, `chatguard.permit.<p>`, `chatguard.warn.<p>` with
`p = sha256(community:user_uuid)`; `.`-separated, never `:`.

## Output safety

Replies carry integers, static text and the host-minted `{user:<token>}` placeholder (validated,
then kept live through `sanitize_user_text`'s allow-list); blacklist words are never echoed; the
bot's own warnings and replies are tested not to trip any filter (no moderation loop).

## Permissions (V2)

`storage.kv`, `identity.resolve`, `chat.delete:discord`, `chat.send:discord`, `chat.send:twitch`.

## Gating

Activation-gated only -- no feature flag (installed -> enabled -> activated); every filter also
starts disabled per community until a moderator enables it.

## Registration (not done in this change)

Needs a `bundles/core-bundles.yaml` row with **`wit_world: stage-next`** (`identity.resolve` is
stage-next-only; the build script fails on a `stage` row):

```yaml
  - app_id: waddles.core.example.chatguard
    version: "1.0.0"
    language: python
    wit_world: stage-next
    source_dir: python/chatguard
    manifest_path: chatguard.manifest.yaml
    artifact_path: chatguard.wasm
    activation_targets:
      - tenant_slug: global
        community_id: null
```

`chat.delete:discord` is `dangerous`; the seeder auto-grants dangerous permissions tenant-wide at
activation (merge-time product decision, same as the other dangerous-permission bundles).

## What the filters see (security review)

The filters analyse the text as the platform RENDERS it, not as it was typed:

- **No prefix cut-off.** The whole message is analysed up to `MAX_ANALYSIS_CHARS` (4096, above
  every platform limit: Twitch 500, Discord 2000 / 4000 with Nitro). A longer line cannot occur
  legitimately, so it is not analysed at all and counts as breaking the first enabled rule (a
  permitted author skips `links`) -- padding past a cut-off never hides a link, word or caps.
- **Typed braces are text.** The inbound pipeline escapes every `{ } \` an author types, so only
  an UNescaped `{user:<token>}` is a host-minted mention (blanked, its display name is not the
  author's text). The view is built in ONE pass that reads escapes exactly like the egress
  detokenizer (`waddle_sdk.safe_text._split_live` is the fuzzed oracle in the tests); a typed
  `{user:evil.com}` is therefore still seen by the link / blacklist / caps / symbol filters.
- **Blank characters are dropped everywhere**: zero-width/format/combining characters (Unicode
  `Cf`/`Mn`/`Cc`) plus the Hangul fillers and the Braille blank (categories the platform renders as
  empty space). They can neither split a word, nor dilute the caps/symbol percentage.
- **Domains may use any script** (`evil` with a Cyrillic `i`, an all-Cyrillic label): platforms
  linkify them. A backslash before punctuation (`evil\.com`, Discord renders `evil.com`) is dropped
  in the link view. A URL is only exempt from the caps/symbol statistics for its first 200
  characters.
- **A non-moderator's `!chatguard ...` / `!permit ...` line is still screened** before the command
  is looked at -- it used to turn into a "denied" reply and skip every filter.

Known gaps (host/platform, not closable in the bundle): the data plane delivers no message-EDIT
events, so a clean message edited afterwards is never re-screened; the blacklist is NFKD +
casefold + whole-word, with no confusables table (a Cyrillic `a` still splits `badword`); with the
`waddles.core.disable-pii-tokenization` kill-switch ON the host neither escapes typed braces nor
mints placeholders, so a typed `{user:token-shaped-word}` is blanked like a mention.

## Limits / follow-ups

- Link detection is a spam heuristic (TLD list + obfuscation normalization), not a URL parser;
  Twitch emote names are plain words in the text and cannot be counted as emotes (only Unicode
  emoji and Discord custom emotes are).
- A timeout capability (see above) and Twitch delete are host work; escalation (warn -> timeout)
  needs the former.
- Per-user exemptions (regulars/subscribers) and a link domain allow-list are not implemented.
