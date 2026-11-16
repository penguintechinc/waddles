# Music queue page (webui)

Route `/community/:communityId/music` (numeric id), served by `admin/hub_module/frontend/src/pages/community/MusicQueue.tsx`.

- Data: `GET /api/v1/communities/{id}/music` (see `music-requests-service.md`), polled every 15s, paused while the tab is hidden.
- Flag: `waddles.music.queue-page` (default OFF; allowlisted in `hub_api/blueprints/v1/flags.py`). Off shows "Page not found"; the sidebar "Music" link is hidden. An API 404 also shows not-found.
- Shows: Now Playing, Up Next, Last 5 Played, Top artists / Top songs (top 5), "AI DJ: On/Off" and "DMCA-safe mode: On/Off" text badges (on = only royalty-free and NCS tracks).
- Safety: parsers copy a field allowlist, so a requester field is never rendered even if sent; `source_url` becomes a link only when it is https (`rel="noopener noreferrer"`); all text is rendered as React text, never HTML.
- A11y: one `h1`, section headings, `aria-live="polite"` region, text labels on badges, axe-checked in tests.
