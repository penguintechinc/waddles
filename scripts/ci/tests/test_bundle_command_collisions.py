"""Gate: two bundles must never answer the same chat command.

Every bundle declares the commands it answers as `command_prefix` filters on its `consumes`
entries (`bundle.yaml`). Two bundles on one trigger both reply to every line, so a community that
activates both gets a doubled answer (and, for mutating commands, a doubled effect). A bundle
that deliberately sees EVERY chat line (no `command_prefix` filter) still answers specific heads;
those are declared in `NO_FILTER_HEADS` below and checked against the same table.

This gate proves, over EVERY `bundles/python/*/bundle.yaml`:

1. **No two bundles share a trigger**, except the pre-existing pairs in `KNOWN_SHARED` (each with
   its reason). The allowlist is exact: an entry that no longer collides fails, so it can only
   shrink -- it is not a place to park a new collision (rename the new bundle's command instead,
   the way `queue` became `!vqueue` next to `music`'s `!queue`).
2. **The no-filter bundles' heads are real and unique**: each head in `NO_FILTER_HEADS` appears
   in that bundle's `src/app.py` (so the table cannot rot) and collides with no declared prefix.
   A bundle with no `command_prefix` filter that is missing from the table fails -- it must say
   which heads it answers.
3. **Denominators.** Fewer than 100 bundles / 100 declared triggers scanned FAILS: a mis-pointed
   glob must not pass vacuously.

Run: python3 -m pytest scripts/ci/tests/test_bundle_command_collisions.py
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLES_DIR = REPO_ROOT / "bundles" / "python"

#: Heads answered by bundles that consume every chat line (no `command_prefix` filter). `[]` =
#: passive (acts on ordinary chat, answers no command). `hypetrain` only COUNTS `!hype` as a
#: keyword; the `hype` bundle owns and answers it, so it is not listed as a head here.
NO_FILTER_HEADS: dict[str, tuple[str, ...]] = {
    "chatguard": ("!chatguard", "!permit"),
    "greeter": ("!greeter",),
    "hypetrain": ("!hypetrain",),
    "keywordgiveaway": ("!kgiveaway",),
    "level": ("!level",),
    "timer": ("!timer",),
}

#: Triggers already shared before this gate existed, with the bundles on them. Exact: a pair that
#: stops colliding must be removed from here.
KNOWN_SHARED: dict[str, frozenset[str]] = {
    "!bot": frozenset({"about", "bot"}),
    "!highfive": frozenset({"highfive", "social"}),
    "!hug": frozenset({"hug", "social"}),
    "!pat": frozenset({"pat", "social"}),
    "!ship": frozenset({"love", "ship"}),
}


def _load() -> tuple[dict[str, set[str]], set[str], int]:
    """`(trigger -> bundles, bundles with no command_prefix filter, bundles scanned)`."""
    triggers: dict[str, set[str]] = defaultdict(set)
    unfiltered: set[str] = set()
    scanned = 0
    for manifest in sorted(BUNDLES_DIR.glob("*/bundle.yaml")):
        scanned += 1
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        found = False
        for stage in (data.get("stages") or {}).values():
            for consume in stage.get("consumes") or []:
                for prefix in (consume.get("filters") or {}).get("command_prefix") or []:
                    triggers[str(prefix).strip().lower()].add(manifest.parent.name)
                    found = True
        if not found:
            unfiltered.add(manifest.parent.name)
    return triggers, unfiltered, scanned


TRIGGERS, UNFILTERED, SCANNED = _load()


def test_the_scan_has_a_real_denominator() -> None:
    assert SCANNED >= 100, f"only {SCANNED} bundle manifests scanned -- wrong root?"
    assert len(TRIGGERS) >= 100, f"only {len(TRIGGERS)} triggers found -- manifest shape changed?"


def test_no_two_bundles_answer_the_same_trigger() -> None:
    shared = {t: frozenset(b) for t, b in TRIGGERS.items() if len(b) > 1}
    new = {t: sorted(b) for t, b in shared.items() if KNOWN_SHARED.get(t) != b}
    assert not new, (
        f"bundles share a command trigger (both reply to every line): {new} -- rename the NEW "
        "bundle's command (see `queue` -> `!vqueue`), do not add it to KNOWN_SHARED"
    )


def test_the_known_shared_allowlist_is_exact() -> None:
    stale = {t for t, bundles in KNOWN_SHARED.items() if frozenset(TRIGGERS.get(t, ())) != bundles}
    assert not stale, f"KNOWN_SHARED entries that no longer match the manifests: {sorted(stale)}"


def test_every_bundle_without_a_prefix_filter_declares_its_heads() -> None:
    assert UNFILTERED == set(NO_FILTER_HEADS), (
        f"bundles that consume every chat line must be listed in NO_FILTER_HEADS: "
        f"unlisted={sorted(UNFILTERED - set(NO_FILTER_HEADS))} "
        f"stale={sorted(set(NO_FILTER_HEADS) - UNFILTERED)}"
    )


@pytest.mark.parametrize(("bundle", "heads"), sorted(NO_FILTER_HEADS.items()))
def test_a_no_filter_bundles_heads_are_real_and_collide_with_nothing(
    bundle: str, heads: tuple[str, ...]
) -> None:
    source = (BUNDLES_DIR / bundle / "src" / "app.py").read_text(encoding="utf-8").lower()
    for head in heads:
        assert head in source, f"{bundle}: head {head!r} not found in its app.py (stale table)"
        owners = TRIGGERS.get(head, set()) | {
            other for other, theirs in NO_FILTER_HEADS.items() if head in theirs
        }
        assert owners == {bundle}, f"{bundle}: head {head!r} is also answered by {sorted(owners)}"


def test_the_queue_rename_is_in_effect() -> None:
    """`music` owns `!queue`; the viewer queue answers `!vqueue` (the trigger that was renamed)."""
    assert TRIGGERS["!queue"] == {"music"}
    assert TRIGGERS["!vqueue"] == {"queue"}
