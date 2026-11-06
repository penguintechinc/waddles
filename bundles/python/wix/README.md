# wix (`waddles.core.integration.wix`, #485)

All Wix features in one app bundle. Wix is a provider name; nothing here is customer-specific.

- `!wix [status [@user]]`, `!wix set @user <wix-member-uuid>` (mod), `!wix remove [@user]`, `!wix enable|disable roles|events` (mod).
- Wix REST only: `net.http.fqdn:www.wixapis.com` GET (`WIX_MEMBER_URL`, a provider constant); key/site id via secret-refs `WIX_API_KEY` / `WIX_SITE_ID`. Only `member.status` is read. Wix endpoint and status enum are assumptions the customer must confirm.
- PII: identities via `identity.resolve` (community UUIDs); Wix ids are opaque UUIDs keyed by that UUID; no name/email/handle stored, logged or echoed.
- Professional-tier gated (`tier_at_least`, fails closed). World `stage-next`. Activation-gated.
- Separate default-off feature modules `src/plan_roles.py` (plan -> Discord role) and `src/event_sync.py` (Wix events -> community events): they need the Wix webhook connector and the generic table-access grant; `!wix enable <module>` is refused loudly until the runtime provides the capability.
- Announcements: use the generic `waddles.core.example.announce` bundle.
- Test: `python3 -m pytest bundles/python/wix/tests`
