# community_feed (`waddles.core.community.feed`, #484)

Generic read-only public-feed reader: `!feed [events]`, `!feed event <n|id>`, `!feed info [topic]`, `!feed sync` (mod), `!feed set url <https-url>` / `!feed reset url` (mod).

- The feed url is per-community config (validated https/hostname-only), not a hardcoded host. Wildcard fqdn grants are rejected by the capability gate, so the operator grants `net.http.fqdn:<host>` for the chosen host; without a grant the call is denied and the reply says unavailable (fail closed).
- GET only, no credentials; last good snapshot cached per community in kv (`feed.snapshot`, TTL 5 min); stale served with an explicit marker.
- No write/admin scope. Feed text is untrusted: validated, capped, sanitised (`safe_text`), reply lead-guarded. Logs carry counts/status only.
- Feed schema: see `src/app.py` docstring. Activation-gated (OFF until activated).
- Test: `python3 -m pytest bundles/python/community_feed/tests`
