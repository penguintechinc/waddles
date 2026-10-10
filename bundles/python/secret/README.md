# secret (Python)

`!secret <username> <message>` -- one-time secret messaging (feature #684). First-party
(`provider: builtin`, `app_id: waddles.core.example.secret`, `author: PenguinTech/waddles`,
Apache-2.0), **original Waddles content** (not a port; no third-party attribution applies).

The sender's chat message is deleted, the secret is stored by hub-api as a **single-use** secret,
and the target is DM'd a link to the webui pull page. The plaintext never appears in chat after the
delete, never in a log, and never in the DM itself.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!secret <username> <message>` | any member (Discord only) | Stores `<message>` for `<username>`, deletes the sender's original message, DMs the target `<webui>/secret#<token>`, and replies `Secret delivered by DM.` |

`<username>`: optional leading `@`, 1-32 chars of letters/digits/`_`/`.`/`-` (starting with a
letter/digit/underscore), case-insensitive. `<message>`: 1-**4000** chars, trimmed at the ends only.
Anything else (`!secret`, `!secret bob`, a bad name, an over-long message) replies
`Usage: !secret <username> <message>` -- the malformed text is **never echoed**.

```text
alice> !secret @bob the launch code is 4242
         (alice's message is deleted)
bot>    Secret delivered by DM.
bob (DM)> You have a one-time secret message. It can be opened once: https://<webui>/secret#<token>
```

## Flow (order matters)

```
validate + resolve target (identity index) + platform supports delete & DM   [no side effects yet]
  -> store via hub-api POST /api/v1/one-time-secrets        (#718)
  -> chat.delete the original message, require CONFIRMATION
  -> dm.send the link (token in the URL fragment), require CONFIRMATION
  -> reply to the sender with a generic notice
```

`relay.push` for `chat.delete`/`dm.send` returns only after the platform **confirmed** the op and
raises otherwise, so a normal return *is* the confirmation. The original message is never deleted
before the secret is stored, and the DM is never sent unless the delete was confirmed.

| Failure step | Sender is told | Side effects |
|---|---|---|
| `unsupported_platform` (Twitch) | "isn't supported on this platform; nothing was sent." | none |
| `target_unlinked` / `resolve` / `index_corrupt` | "That user isn't linked for secret messaging; nothing was sent." | none |
| `missing_context` / `bad_community` / `store` / `store_status` / `store_body` | "Couldn't store the secret; your message was left as-is." | none (nothing stored, nothing deleted) |
| `delete` | "...the secret was NOT delivered; please delete your message yourself." | secret stored, link never shared (it expires) |
| `dm` | "Your message was removed but the DM failed; please resend." | message deleted, secret expires |

Every failure is replied (generic text only), logged (step name only), and then **re-raised**
(`SecretFlowError`) so the host sees it -- never swallowed.

## Identity (limitation until #429)

`<username>` resolves through a community-scoped kv index
`secret.target.<sha256(lower(name))>` -> JSON `{"uuid": <hub user uuid>, "platform_user_id": <id>}`
(`c.<community_id>.` prefixed, `.`-separated, never `:` -- gh-631). It must be populated by the
identity-link flow; an **unlinked target fails loud** rather than guessing. A corrupt entry
(non-UTF-8 / non-JSON / missing field) fails loud as `index_corrupt` and is never overwritten.

## Permissions (V2 structured)

| Id | Why |
|---|---|
| `chat.delete:discord` | Deletes the sender's original `!secret` chat message so the secret text does not stay public. |
| `dm.send:discord` | DMs the target the single-use link (token in the URL fragment). The host only DMs members of the originating guild and throttles per community/app and per recipient. |
| `chat.send:discord` | Replies to the sender with a generic success/failure notice (never the secret). |
| `net.http.fqdn:hub-api.penguintech.cloud` (`POST`) | Calls the hub-api one-time-secret create endpoint (scope `secret_messaging:create`). |
| `storage.kv` | Reads the community's username -> hub-user identity index (`secret.target.*`). |
| `flags.read` | Gates the command behind `waddles.command-secret` and `waddles.secret-messaging`. |

Egress is exactly `hub-api.penguintech.cloud` `POST`; the `net.http.fqdn` grant host must match
`hub_api_url`'s host -- adjust both per deployment. The service credential is the stage secret-ref
`SECRET_MESSAGING_SERVICE_TOKEN` (value `Bearer <jwt>`): it is passed as a `secret_refs` **name**,
never as a header/body value, and never enters the guest. The chat/DM ops depend on host connector
support (see #719).

## Config

| Key | Purpose |
|---|---|
| `hub_api_url` | Base URL of hub-api (trailing slash tolerated). A missing key fails the flow loudly at the `store` step. |
| `webui_url` | Base URL of the webui serving the `/secret` pull page (#721). Required to build the DM link. **Known limitation:** it is only read *after* the delete, so a missing key (a `required_config` contract violation, enforced at activation) surfaces as a raw `KeyError` with the message already deleted and no generic notice sent. |

## Feature flags

Both **default OFF** and are required: `waddles.command-secret` (this command) **and**
`waddles.secret-messaging` (also gates the hub-api endpoint). Each is requested with
`default=False`, so a flag outage or missing `wit_world` keeps the command off. While off nothing is
parsed, logged or sent.

## Platforms

`consumes` **Discord** and **Twitch** `chat.message` with `command_prefix: ["!secret"]`, but only
**Discord** is supported (`SUPPORTED_PLATFORMS`): Twitch `chat.delete`/`dm.send` are `Unsupported`
stubs, so a Twitch invocation is refused up front with nothing stored or deleted. A `community`
context is required.

## Logging / PII

Logs carry only `op` / `step` / `platform` / `community_id` / exception **type** / HTTP status --
never the secret, the username, the target UUID or platform id, the link token, or any host error
text. Public replies are generic for the same reason. Regression:
`tests/test_backfill.py::test_no_log_line_in_any_failure_path_contains_secret_text_username_token_or_uuid`.

## Files

| File | Role |
|---|---|
| `bundle.yaml` / `hub-manifest.yaml` | Manifest (egress + permissions) / hub-api install-pipeline manifest |
| `src/app.py` | `transform` (parse) / `dispatch` (ordered flow + generic reply) |
| `src/_entry_wiring.py` | Static entry wiring (see `pyping`'s) |
| `tests/` | Host-native pytest suite (fake `wit_world`: flags/relay/kv/log + a fake HTTP client) |

## Test

```bash
cd bundles/python/secret
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0
pytest --cov=src --cov-branch --cov-report=term-missing   # 100% line + branch
```

`test_app.py` covers the ordered happy path and the headline failure modes (unconfirmed delete/DM,
unsupported platform, unlinked target); `test_backfill.py` covers every remaining failure step,
corrupt identity index, malformed store responses, input bounds, flag gating, credential handling
and the PII-free-log regression.
