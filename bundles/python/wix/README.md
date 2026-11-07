# wix (`waddles.core.integration.wix`, #485)

All Wix features in one app bundle. Wix is a provider name; nothing here is customer-specific.

- `!wix [status [@user]]`, `!wix set @user <wix-member-uuid>` (mod), `!wix remove [@user]`, `!wix enable|disable roles|events` (mod).
- Wix REST only: `net.http.fqdn:www.wixapis.com` GET (`WIX_MEMBER_URL`, a provider constant); key/site id via secret-refs `WIX_API_KEY` / `WIX_SITE_ID`. Only `member.status` is read. Wix endpoint and status enum are assumptions the customer must confirm.
- PII: identities via `identity.resolve` (community UUIDs); Wix ids are opaque UUIDs keyed by that UUID; no name/email/handle stored, logged or echoed.
- Free first-party bundle: no tier/licence gate. World `stage-next-tools` (the `tool` import). Activation-gated.
- Default-off feature modules (`src/plan_roles.py`, `src/event_sync.py`), driven by the Wix webhook connector (`waddles.core.service.wix`) and calling the Discord SERVICE tools under approved `tool.call:` grants (never Discord HTTP):
  - `!wix plan map <plan-uuid> <role_key>` / `!wix plan unmap <plan-uuid>` (mod) + `!wix enable roles`: `subscription.started|changed|ended` -> `discord.member.roles.add|remove`.
  - `!wix enable events`: `event.created|updated|canceled` -> `discord.scheduled_event.create|update|cancel`; the Wix->Discord id link is kv `wix.evmap.<wix_event_id>` (redelivery is a no-op).
  - Fail loud: `!wix enable <module>` is refused if the runtime lacks the `tool` import; any tool failure (not granted, forbidden, unavailable...) is logged AND recorded under kv `wix.fault.<module>`; `!wix modules` (mod) shows each module's on/off, `TOOLS MISSING`, and last failure code.
  - Ordering guard (delivery is at-least-once/unordered): kv `wix.sub.<order_id>` / `wix.evst.<wix_event_id>` hold `occurred_at|status|clock`. REVOCATIONS ALWAYS WIN: role removes and event cancels are never timestamp-gated (only skipped when the order/event is already terminal) and mark it terminal (ENDED/CANCELED), so nothing re-grants/resurrects it -- only a NEW order id does. GRANTS (role add, event create/update) require a Wix-sourced timestamp (connector payload `time_source: "wix"`) strictly newer than a Wix-sourced stored one; receive-time fallbacks (`time_source: "received"`) are non-orderable, never compared across clocks, and a grant on them is refused loudly (`unorderable_grant`/`unorderable_event`, shown by `!wix modules`). State advances only after the tool call succeeds. Limitation: current state is not re-confirmed against Wix (no `wix.order.get` tool yet).
  - Outbound Wix writes (`wix.event.*`) are the Rust service bundle's tools, consumed by the calendar app, not by this bundle.
- Announcements: use the generic `waddles.core.example.announce` bundle.
- Test: `python3 -m pytest bundles/python/wix/tests`
