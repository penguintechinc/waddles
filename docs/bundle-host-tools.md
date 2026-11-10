# Host-executed platform tools (`exported_tools[].host_op`)

A platform SERVICE bundle (twitch / discord / youtube / kick) declares tools such as
`twitch.moderation.ban` whose execution is **host-side**: the guest `call` refuses them, the
stage translates an authorized `tool.invoke` into a pinned producer op that svc_ingest's
`sender_exec` executes against the platform. The translation is data, not code: the bundle's
manifest names the registered op kind and the argument mapping
(`core/svc_action/src/capabilities/host_tools.rs`).

```yaml
exported_tools:
  - name: twitch.moderation.timeout
    access: write
    risk: dangerous
    required_scope: community:moderate     # enforced against the host-VERIFIED actor
    input_schema: { ... }
    output_schema: { ... }
    host_op:
      platform: twitch                     # twitch | discord | youtube | kick
      op: mod.timeout                      # must be in bundle_capability_gate::platform_ops
      user: user                           # tool arg (tokenized UUID) -> wire user_id
      user_wire: platform_id               # platform_id (default, detokenized host-side) | placeholder
      text_object:                         # json key -> tool arg; becomes the wire `text`
        duration: duration_seconds
        reason: reason
      required: [user, duration_seconds]
      origin: channel                      # channel (default) | guild_scope | none
      returns: [id]                        # platform reply keys surfaced to the caller
```

Other mapping keys: `message_id` (tool arg -> wire `message_id`), `text` (string arg -> wire
`text`), `text_list` (string-array arg -> sorted, de-duplicated, comma-joined `text`).
`text`, `text_list` and `text_object` are mutually exclusive. Unknown keys are rejected at
manifest parse; any tool argument the declaration does not map is refused at call time.

## Pipeline

1. Planner (`bundle_tool_call::authorize_tool_call`): caller `requested_tools`, `tool.call`
   grant, write key, then the ACTION stage resolves the verified actor
   (`svc_action::actor`: event author -> active community member -> role scopes) and enforces
   `required_scope` (`scope_denied`).
2. Args validated against the declaration (closed set; no guild/channel/credential/id).
3. Origin pinned from the verified event (`no_bound_channel` when absent); the platform
   SERVICE bundle must itself hold the op's catalog permission.
4. Drain readiness, per-community throttle, then **user detokenization** host-side via hub-api
   `ResolvePlatformUser` (`no_linked_account` / fail closed) and the **protected-target** check
   (`protected_target`: owner / admin / moderator of the community, for ban/timeout/kick).
5. Signed (`pin_sig`) envelope queued; the tool waits for the consumer's ack (bounded;
   silence = `not_confirmed`, never "done"). Only manifest-`returns` keys of the reply reach
   the caller -- a created webhook's token never does.

## Testing a platform bundle's declarations

Planner tests must run the bundle's REAL declarations, not hand-built copies. The consent summary of
a platform bundle's `exported_tools` lives at
`core/svc_action/tests/fixtures/host_tools/<platform>.json`; `scripts/host_tool_summary.py
<bundle-dir> --write <fixture>` regenerates it through the production manifest parser and
`hub_api/tests/test_platform_host_tool_fixtures.py` fails when either `bundle.yaml` or
`hub-manifest.yaml` drifts from it. Rust tests load it with `manifest_tools(<platform>)`
(`host_tools_tests.rs`) and call tools through `StageCapabilities::handle`
(`tool.invoke`), e.g. `host_tools_twitch_manifest_tests.rs` and `host_tools_discord_manifest_tests.rs`.

A `user_wire: placeholder` user (`{user:<uuid>}`, resolved by the consumer) is accepted only for the typed role ops
and `mod.*` ops; for destructive moderation ops the planner still detokenizes the user host-side to run the
protected-target check before queueing the placeholder.

## Stable refusal codes

The guest-facing `tool-error` is a closed WIT enum, so the code is the pinned **prefix** of the
refusal message on `forbidden` / `invalid_args`: `protected_target`, `no_linked_account`,
`no_bound_channel`, `not_supported`, `scope_denied` (`"<code>: <detail>"`). Map on the prefix.

## Transitional declarations

The Discord bundle's member-role and scheduled-event tools predate `host_op`; their
declarations live in `TRANSITIONAL_DECLS` (host_tools.rs) and a manifest `host_op` always wins.
Delete the table once the Discord bundle declares them.
