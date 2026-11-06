# barcitizen_full (`waddles.core.barcitizen.full`, #485)

Bar Citizen full (read/write) Discord module, Wix instead of Odoo (owner decision, design spec 2026-09-29 Sec 2.2).

- `!bcm [status [@user]]`, `!bcm link @user <wix-member-uuid>` (mod), `!bcm unlink [@user]`, `!bcm news`, `!bcm announce <text>` (mod).
- Wix only via its REST API: `net.http.fqdn:www.wixapis.com`, GET only; key/site id via secret-refs `WIX_API_KEY` / `WIX_SITE_ID` (operator-provisioned, never in the repo). Only `member.status` is read.
- PII: identities via `identity.resolve` (community UUIDs); Wix ids are opaque UUIDs keyed by that UUID; no name/email/handle is stored, logged or echoed.
- World `stage-next` (identity). Activation-gated + Professional-tier gated (fails closed).
- **Follow-ups, not in this bundle:** Wix webhooks (host-side connector transport + signature/replay verification), Wix events -> event-sync, pricing-plan -> Discord role (rides role sync).
- **ASSUMPTIONS:** Wix `members/v1/members/<id>` endpoint + `status` enum (APPROVED/PENDING/BLOCKED/OFFLINE) per Wix docs recall; Professional tier gate IS enforced (license-tier assumption from spec open question 7 -- customer to confirm).
- Test: `python3 -m pytest bundles/python/barcitizen_full/tests`
