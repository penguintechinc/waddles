# barcitizen_readonly (`waddles.core.barcitizen.readonly`, #484)

Read-only Bar Citizen community events + public info: `!bc [events]`, `!bc event <n|id>`, `!bc info [topic]`, `!bc sync` (mod).

- Pulls Bar Citizen's public JSON feed (GET only, no credentials, `net.http.fqdn:barcitizen.org`) lazily, caches the last good snapshot per community in kv (`bcro.snapshot`), TTL 5 min; stale cache is served with an explicit marker, never silently.
- No write/admin scope: permissions are `storage.kv` (cache), the GET egress, and `chat.send:*` for the reply only.
- Feed text is untrusted: validated, capped, sanitised (`safe_text`), reply lead-guarded. Logs carry counts/status only.
- Activation-gated (OFF until a community admin activates it) -- the repo's command-bundle convention.
- **ASSUMPTION**: feed host/path (`FEED_URL` in `src/app.py`, plus the manifest egress host) are a placeholder until Bar Citizen confirms its real source (spec open question 1).
- Test: `python3 -m pytest bundles/python/barcitizen_readonly/tests`
