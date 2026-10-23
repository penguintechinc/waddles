# wouldyourather

`!wyr` (alias `!wouldyourather`) replies with a random SFW "Would you rather X or Y?" prompt
from a static list. Stateless relay (no kv/db). Malformed input (`!wyr foo`) gets a usage reply.

- Activation-gated only (installed -> enabled -> activated); no feature flag.
- Permissions: `chat.send:discord`, `chat.send:twitch`.
- Logging is PII-free: only the platform name is logged; input and `event.actor` never are.
- Tests: `cd bundles/python/wouldyourather && python3 -m pytest -q`.
