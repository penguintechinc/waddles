#!/usr/bin/env python3
"""Generates bundle.yaml, hub-manifest.yaml and README.md for the SuperPenguinTV wave-3 utility ports.

One table (`BUNDLES`) drives all three files so the two manifests can never drift. The MIT notice is the upstream LICENSE
verbatim (taken from bundles/csharp/superpenguin-first/LICENSE, itself a verbatim copy of Psychoboy/PenguinTwitchBot's).
Usage: python3 scripts/superpenguin-scaffold/gen_manifests.py   (from the repo root; rewrites the three files per bundle)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CS = ROOT / "bundles" / "csharp"
ECONOMY = "waddles.core.service.economy"
REPUTATION = "waddles.core.service.reputation"
CHAT = [
    {
        "id": "chat.send:discord",
        "justification": "replies to the invoking user/channel via the relay host import (relay.push -> chat.send:discord)",
    },
    {
        "id": "chat.send:twitch",
        "justification": "replies to the invoking user/channel via the relay host import (relay.push -> chat.send:twitch)",
    },
]


def KV(why: str) -> dict[str, str]:
    """A `storage.kv` permission with its justification."""
    return {"id": "storage.kv", "justification": why}


def IDENTITY(why: str) -> dict[str, str]:
    """An `identity.resolve` permission with its justification."""
    return {"id": "identity.resolve", "justification": why}


def chat_consumes(
    prefixes: list[str] | None, raid_event: bool = False
) -> list[dict[str, Any]]:
    """Chat consumption on both platforms; `prefixes=None` means every chat message (passive trackers)."""
    out: list[dict[str, Any]] = []
    if raid_event:
        out.append({"platform": "twitch", "event_types": ["channel.raid"]})
    for platform in ("twitch", "discord"):
        entry: dict[str, Any] = {"platform": platform, "event_types": ["chat.message"]}
        if prefixes:
            entry["filters"] = {"command_prefix": prefixes}
        out.append(entry)
    return out


def tool(target: str, name: str, write: bool, why: str) -> dict[str, Any]:
    """A `tool.call` permission plus its `requested_tools` row."""
    return {
        "perm": {
            "id": f"tool.call:{target}:{name}",
            "justification": why,
            "params": {"write": write},
        },
        "req": {
            "tool": name,
            "access": "write" if write else "read",
            "target_app_id": target,
        },
    }


BUNDLES: dict[str, dict[str, Any]] = {
    "lastseen": {
        "title": "Last Seen",
        "upstream": "Misc/LastSeen.cs + Features/ViewerFeature.cs (last-seen stamp)",
        "consumes": chat_consumes(None),
        "perms": [
            KV("per-user last-seen stamp (90-day TTL)"),
            IDENTITY(
                "resolve the speaker and @mention targets to community user uuids (uuid-only, no names)"
            ),
        ],
        "tools": [],
        "egress": [],
        "timeout_ms": 2000,
        "summary": "`!lastseen [@mention]` reports how long ago a community member last chatted. Every chat message stamps the speaker.",
        "commands": [
            "`!lastseen` - your own previous activity",
            "`!lastseen @name` - that member's last activity",
        ],
        "limits": [
            "Targets are @mentions (UUIDs); the original looks up by Twitch username, which a bundle never sees.",
            "Stamps expire after 90 days; an expired stamp reads as never seen.",
            "Needs every chat message delivered (no command-prefix filter in the manifest).",
        ],
    },
    "deathcounter": {
        "title": "Death Counter",
        "upstream": "Misc/DeathCounters.cs",
        "consumes": chat_consumes(["!death"]),
        "perms": [
            KV("per-game-label death counters and the active game label"),
            IDENTITY("resolve the moderator for the per-invocation replay claim"),
        ],
        "tools": [],
        "egress": [],
        "timeout_ms": 2000,
        "summary": "`!death` reports the streamer's death count; moderators and the broadcaster change it.",
        "commands": [
            "`!death` - show the counter",
            "`!death +` / `-` / `reset` / `set N` - moderators and the broadcaster",
            "`!death game NAME` - set the active game label (bare `!death game` clears it)",
        ],
        "limits": [
            "The original keys the counter on the live Twitch game; a bundle cannot read it, so a moderator sets the active game label.",
            'The reply says "The streamer" (no broadcaster identity reaches a bundle).',
            "No overlay text file / SignalR push; use the platform overlay service.",
        ],
    },
    "dailycounter": {
        "title": "Daily Counter",
        "upstream": "Misc/DailyCounter.cs",
        "consumes": chat_consumes(
            ["!dailycounter", "!setdailycountertext", "!setdailyvalue", "!dailyadd"]
        ),
        "perms": [
            KV("the counter value and its display format"),
            IDENTITY(
                "resolve the invoking moderator for the per-invocation replay claim"
            ),
        ],
        "tools": [],
        "egress": [],
        "timeout_ms": 2000,
        "summary": "A streamer-facing daily counter with a `{value}` / `{nextgoal}` format, as in the original.",
        "commands": [
            "`!dailycounter` - show the rendered text",
            "`!setdailycountertext FORMAT` - broadcaster",
            "`!setdailyvalue N` - broadcaster",
            "`!dailyadd [N]` - moderators (stand-in for sub events)",
        ],
        "limits": [
            "The original increments on subscription events and writes Data/DailyCounter.txt; bundles get chat only and have no file system, so `!dailyadd` stands in until a subscription event contract exists.",
            "No compare-and-swap in kv: two simultaneous `!dailyadd` messages can lose one update.",
        ],
    },
    "quotes": {
        "title": "Quotes",
        "upstream": "Misc/QuoteSystem.cs",
        "consumes": chat_consumes(["!quote", "!addquote", "!delquote"]),
        "perms": [
            KV(
                "the quote book (one record per quote, a counter and per-invocation replay records)"
            ),
            IDENTITY("resolve the invoking moderator (quote author uuid)"),
        ],
        "tools": [],
        "egress": [],
        "timeout_ms": 2000,
        "summary": "A community quote book: random, by number, or by text.",
        "commands": [
            "`!quote [N|#N|text]`",
            "`!addquote TEXT` (alias `!quoteadd`) - moderators",
            "`!delquote N` (alias `!quotedel`) - moderators",
        ],
        "limits": [
            "Quote numbers are permanent (a delete leaves a tombstone); the original renumbers.",
            "No game name (Twitch lookup) and the author is a `{user:uuid}` placeholder.",
            "Text search covers the 48 newest quotes (host kv has no listing and a 64-op cap per invoke).",
            "The original's banned-user filter is replaced by moderator deletes.",
        ],
    },
    "raidtracker": {
        "title": "Raid Tracker",
        "upstream": "Misc/RaidTracker.cs",
        "consumes": chat_consumes(["!raid"], raid_event=True),
        "perms": [
            KV(
                "per-raider incoming-raid history (UUID only), the recent-raider index and replay claims"
            ),
            IDENTITY(
                "attribute a raid to a community user uuid when the event actor is not a user token"
            ),
        ],
        "tools": [],
        "egress": [],
        "timeout_ms": 2000,
        "summary": "Records incoming raids (`channel.raid`) per raider and lists the top raiders.",
        "commands": [
            "`!raids` - top five raiders",
            "`!raid TARGET` - broadcaster only: refused loudly (no platform raid tool exists)",
        ],
        "limits": [
            "Outgoing raids need a platform raid tool and Twitch credentials (host-side); not available, so refused loudly.",
            "The online-status poll, last game and prune UI need Twitch lookups and a scheduler.",
            "Only the 50 most recent raiders are kept; unattributable raids go to an aggregate tally.",
        ],
    },
    "top": {
        "title": "Top",
        "upstream": "Misc/Top.cs + Misc/TopPointTypeSelection.cs",
        "consumes": chat_consumes(["!top5", "!top10", "!toprep"]),
        "perms": [],
        "tools": [
            tool(
                ECONOMY,
                "points_leaders",
                False,
                "Use the economy service points_leaders contract; the app owns only command policy and formatting",
            ),
            tool(
                REPUTATION,
                "leaderboard",
                False,
                "Use the reputation service leaderboard contract; the app owns only command policy and formatting",
            ),
        ],
        "egress": [],
        "timeout_ms": 2000,
        "summary": "Leaderboards from the economy and reputation SERVICE bundles.",
        "commands": [
            "`!top5` / `!top10 [currency]` - richest members",
            "`!toprep` - reputation leaders",
        ],
        "limits": [
            "`!toptime`, `!loudest`, `!toptournaments`, `!toptickets` are not ported: no service exposes watch time, message counts or tournament standings. Nothing is faked.",
            "A community has one currency; the optional argument must name it.",
        ],
    },
    "alias": {
        "title": "Command Alias",
        "upstream": "Alias/Alias.cs + Alias/Handlers/*",
        "consumes": chat_consumes(None),
        "perms": [KV("the per-community alias registry (one JSON document)")],
        "tools": [],
        "egress": [],
        "timeout_ms": 2000,
        "summary": "Short aliases for existing commands.",
        "commands": [
            "`!alias set NAME !target [args]` - moderators",
            "`!alias remove NAME` - moderators",
            "`!alias list`",
            "`!NAME args` - answers with the resolved `!target args`",
        ],
        "limits": [
            "No WIT world lets a bundle re-inject an event or call another app bundle, so an alias relays the resolved command text instead of executing it.",
            "Single-step resolution; a target may not be `!alias` or another alias (no loops).",
            "Needs every command message delivered (no command-prefix filter); a non-`!` message costs no kv operation.",
        ],
    },
    "timers": {
        "title": "Announcement Timers",
        "upstream": "Misc/AutoTimers.cs + Models/Timers/TimerGroup.cs",
        "consumes": chat_consumes(None),
        "perms": [
            KV(
                "the timer groups document, the message counter and per-window run claims"
            ),
            IDENTITY(
                "resolve the invoking moderator for the per-invocation replay claim"
            ),
        ],
        "tools": [],
        "egress": [],
        "timeout_ms": 2000,
        "summary": "Announcement timer groups with interval ranges, minimum chat messages, shuffle and repeat.",
        "commands": [
            "`!timer create NAME [minSec maxSec minMessages] [once]`",
            "`!timer addline NAME TEXT` / `delline NAME N`",
            "`!timer shuffle NAME on|off`",
            "`!timer enable|disable|delete|run NAME`",
            "`!timer list`",
        ],
        "limits": [
            "NO SCHEDULER: no WIT world gives an app bundle a timer, so due groups are checked on every chat message (tick-on-event, like the SDK TickScheduler). A quiet channel announces late; one announcement per event; it goes to the channel of the triggering message.",
            "The online-only flag and stream-start counter reset need the stream-online signal that no world exposes.",
            "Announcements are plain chat lines, not the original's action engine.",
            "Needs every chat message delivered (no command-prefix filter).",
        ],
    },
    "weather": {
        "title": "Weather",
        "upstream": "Misc/Weather.cs",
        "consumes": chat_consumes(["!weather"]),
        "perms": [
            {
                "id": "net.http.fqdn:api.weatherapi.com",
                "justification": "fetches the forecast from the WeatherAPI.com HTTPS API; GET only; the API key is a host-resolved secret reference (WEATHER_API_KEY), never in the guest",
                "params": {"methods": ["GET"]},
            }
        ],
        "tools": [],
        "egress": [{"host": "api.weatherapi.com", "methods": ["GET"]}],
        "timeout_ms": 5000,
        "summary": "`!weather [location]` from WeatherAPI.com. No weather service bundle exists, so the app calls the provider through the host http capability.",
        "commands": [
            "`!weather London`",
            "`!weather` - uses the bundle config `default_location` when the host forwards config",
        ],
        "limits": [
            "Operator must provision the `WEATHER_API_KEY` secret; the key is injected host-side into the `key` header and never enters the guest.",
            "The host does not forward app config yet, so a location argument is normally required.",
        ],
    },
    "viewers": {
        "title": "Viewer Presence",
        "upstream": "Features/ViewerFeature.cs (presence + lurk policy)",
        "consumes": chat_consumes(None),
        "perms": [
            KV("the bounded per-community presence table (one JSON document)"),
            IDENTITY(
                "resolve the speaker to a community user uuid (uuid-only, no names)"
            ),
        ],
        "tools": [],
        "egress": [],
        "timeout_ms": 2000,
        "summary": "Active-viewer and lurker presence, as in the original's viewer feature.",
        "commands": [
            "`!lurk` - mark yourself a lurker (silent)",
            "`!viewers` - active viewer count",
        ],
        "limits": [
            "The viewer table, sub/mod/vip flags and follower lookups belong to the platform identity service and Twitch service tools; not duplicated.",
            "At most 200 viewers tracked; refreshes throttled to 30 s per viewer.",
            "Needs every chat message delivered (no command-prefix filter).",
        ],
    },
}


def notice(upstream: str, license_text: str) -> str:
    """The upstream MIT notice, verbatim, under a one-line port attribution."""
    return (
        f"Ported from PenguinTwitchBot ({upstream}, github.com/Psychoboy/\n"
        "PenguinTwitchBot) by Psychoboy (github:Psychoboy), used with permission.\n\n"
        + license_text.strip("\n")
        + "\n"
    )


class _Dumper(yaml.SafeDumper):
    """Block style for multi-line strings so the MIT text stays readable."""


def _str(dumper: yaml.SafeDumper, data: str) -> Any:
    return dumper.represent_scalar(
        "tag:yaml.org,2002:str", data, style="|" if "\n" in data else None
    )


_Dumper.add_representer(str, _str)


def build(
    name: str, spec: dict[str, Any], license_text: str, hub: bool
) -> dict[str, Any]:
    """One manifest (bundle.yaml when `hub` is false, hub-manifest.yaml when true) as an ordered mapping."""
    perms = (
        [dict(p) for p in spec["perms"]]
        + [dict(p) for p in CHAT]
        + [t["perm"] for t in spec["tools"]]
    )
    tools_req = [t["req"] for t in spec["tools"]]
    consumes = spec["consumes"]
    m: dict[str, Any] = {
        "schema_version": 2,
        "app_id": f"waddles.integrations.superpenguin.{name}",
        "name": f"{spec['title']} (SuperPenguinTV)",
        "version": "1.0.0",
        "feature": "waddles.integrations.superpenguin",
        "module": "integrations",
        "provider": "thirdparty",
        "language": "csharp",
        "artifact": "prebuilt" if hub else "component",
        "execution_model": "native" if hub else "wasi-component",
        "is_default": False,
        "author": "github/psychoboy",
        "collection": "superpenguintv",
        "license": "MIT",
        "category": "alternatives",
        "source_url": "https://github.com/Psychoboy/PenguinTwitchBot",
        "notice": notice(spec["upstream"], license_text),
    }
    process: dict[str, Any] = {
        "entry": "waddle:bundle/process-stage#transform",
        "consumes": consumes,
    }
    action: dict[str, Any] = {"entry": "waddle:bundle/action-stage#dispatch"}
    if not hub:
        process |= {"produces": [], "config": {}, "spec": {"required_config": []}}
        action |= {"config": {}, "spec": {"required_config": []}}
    m["stages"] = {"process": process, "action": action}
    m["egress"] = spec["egress"]
    m["data"] = {"tables": []}
    m["limits"] = {"timeout_ms": spec["timeout_ms"], "memory_mb": 64, "egress_rps": 10}
    m["permissions"] = perms
    if hub:
        m["routes_to"] = []
    else:
        m["config_schema"] = {}
        m["compatible_with"] = []
        m["incompatible_with"] = []
        m["platform_compatibility"] = {
            "tested_with": "1.0.0",
            "min_version": "1.0.0",
            "max_version": None,
        }
    m["requested_tools"] = tools_req
    return m


def readme(name: str, spec: dict[str, Any]) -> str:
    """The per-bundle README: what it does, its commands, and its documented limits."""
    lines = [
        f"# superpenguin-{name}",
        "",
        f"C# port of PenguinTwitchBot `{spec['upstream']}` (MIT, Psychoboy; used with permission - see `bundle.yaml` `notice`).",
        f"Collection `superpenguintv`, author `github/psychoboy`. Gated by PostHog flag `waddles.command-superpenguin-{name}` (default OFF).",
        "",
        spec["summary"],
        "",
        "## Commands",
        "",
    ]
    lines += [f"- {c}" for c in spec["commands"]]
    lines += ["", "## Documented deviations and limits", ""]
    lines += [f"- {c}" for c in spec["limits"]]
    lines += [
        "",
        "## App/service boundary",
        "",
        "The app owns command policy and formatting; shared capabilities come from SERVICE bundles through `tool.invoke` (never app to app).",
        "No secrets, tokens or names enter the guest; replies name people with the host `{user:uuid}` placeholder.",
        "State is community-scoped kv (keys use `.`, never `:`). Logs carry ids, counts and outcomes only.",
        "",
        "## Build and test",
        "",
        f"- `make test-superpenguin-{name}` - xUnit + 90% line-coverage floor",
        f"- `make build-superpenguin-{name}-bundle` - pinned Dockerfile to a real `.wasm` component",
        "- See `docs/sdk-cs.md` and `docs/superpenguin-port-inventory.md`.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    license_text = (CS / "superpenguin-first" / "LICENSE").read_text()
    for name, spec in BUNDLES.items():
        d = CS / f"superpenguin-{name}"
        if not d.is_dir():
            print(f"gen_manifests: missing {d}", file=sys.stderr)
            return 1
        for hub, fname in ((False, "bundle.yaml"), (True, "hub-manifest.yaml")):
            text = yaml.dump(
                build(name, spec, license_text, hub),
                Dumper=_Dumper,
                sort_keys=False,
                default_flow_style=False,
                width=100,
                allow_unicode=True,
            )
            (d / fname).write_text(text)
        (d / "README.md").write_text(readme(name, spec))
        print(f"gen_manifests: wrote {name} ({len(BUNDLES)} bundles total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
