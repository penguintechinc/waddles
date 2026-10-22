# commands

`!commands` -- lists the first-party commands this deployment ships, as one chat line (capped at
400 chars, then `(+N more)`). `!commands <anything>` -> `Usage: !commands`.

## How enumeration works (and its limit)

The bundle WIT world has **no capability to list a community's activated bundles/commands**
(only own `kv`/`db`, identity, etc.). So `src/app.py` ships a curated `CATALOG` of first-party
command triggers and lists it as-is. There is **no feature-flag filtering**: command bundles are
activation-gated only (Installed -> Enabled -> Activated); the host routes a command event only to
bundles activated for that community, so a deactivated command's trigger is simply not answered.

**Limitation:** the list means "commands this deployment ships", not "commands activated for this
community" -- activation state is not visible to bundles. A bundle not in `CATALOG`
(third-party/new) is not listed until the catalog is updated. Filtering by activation needs a new
host capability (e.g. `bundles.list-activated`) -- follow-up, not stubbed here.

## Permissions (least privilege)

`chat.send:discord`, `chat.send:twitch`. No flags, no kv, no egress. Activation-gated only.

## Privacy

Logs: platform + command count only; never actor or text. Not a PenguinTwitchBot port (no
attribution needed).
