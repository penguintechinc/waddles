# wix (`waddles.core.integration.wix`, #485)

Event-only app bundle: it exposes **no chat command** (no `chat.message` consume, no `command_prefix`, no `chat.send`). It syncs **Wix events only** (Wix events -> community events). Licensing and subscriptions are handled by the PenguinTech license server, not Wix: this bundle has no plan/subscription/role sync and ignores `subscription.*` records. Wix is a provider name; nothing here is customer-specific.

- Free first-party bundle: no tier/licence gate. World `stage-next-tools` (the `tool` import). Activation is the gate -- once activated event sync runs.
- Feature module (`src/event_sync.py`), driven by the Wix webhook connector (`waddles.core.service.wix`) and calling the Discord SERVICE tools under approved `tool.call:` grants (never Discord HTTP):
  - Event sync: `event.created|updated|canceled` -> `discord.scheduled_event.create|update|cancel`; the Wix->Discord id link is kv `wix.evmap.<wix_event_id>` (redelivery is a no-op).
  - Fail loud: a runtime without the `tool` import records `capability_missing`; any tool failure (not granted, forbidden, unavailable...) is logged AND recorded under kv `wix.fault.<module>`.
  - Ordering guard (delivery is at-least-once/unordered): kv `wix.evst.<wix_event_id>` hold `occurred_at|status|clock`. CANCELS ALWAYS WIN: event cancels are never timestamp-gated (only skipped when the event is already terminal) and mark it CANCELED, so nothing resurrects it. Create/update require a Wix-sourced timestamp (connector payload `time_source: "wix"`) strictly newer than a Wix-sourced stored one; receive-time fallbacks (`time_source: "received"`) are non-orderable, never compared across clocks, and a create/update on them is refused loudly (`unorderable_event`). State advances only after the tool call succeeds.
  - Outbound Wix writes (`wix.event.*`) are the Rust service bundle's tools, consumed by the calendar app, not by this bundle.
- Announcements: use the generic `waddles.core.example.announce` bundle.
- Test: `python3 -m pytest bundles/python/wix/tests`
