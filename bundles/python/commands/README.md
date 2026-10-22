# commands

`!commands` -- lists the commands available in this community, as one chat line (capped at 400
chars, then `(+N more)`). `!commands <anything>` -> `Usage: !commands`.

## How enumeration works (and its limit)

The bundle WIT world has **no capability to list a community's activated bundles/commands**
(only `%flags.enabled(key, default)` per key, own `kv`/`db`, identity, etc.). So:

1. `src/app.py` ships a curated `CATALOG` of first-party command triggers.
2. Each is filtered by its `waddles.command-<name>` flag used as a **kill-switch**:
   explicitly OFF -> hidden; unset -> shown (`default=True`).

**Limitation:** the list means "commands this deployment ships and hasn't switched off", not
"commands activated for this community" -- activation state is not visible to bundles. A bundle
not in `CATALOG` (third-party/new) is not listed until the catalog is updated. A true answer
needs a new host capability (e.g. `bundles.list-activated`) -- follow-up, not stubbed here.

## Permissions (least privilege)

`flags.read`, `chat.send:discord`, `chat.send:twitch`. No kv, no egress. Activation-gated only.

## Privacy

Logs: platform + command count only; never actor or text. Not a PenguinTwitchBot port (no
attribution needed).
