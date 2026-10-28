"""Gate: a game bundle must never draw an outcome from the module-level `random` generator.

componentize-py wizens the bundle at BUILD time and freezes the interpreter state into the
snapshot. The module-level `random` functions share one Mersenne Twister that is seeded when
`random` is imported -- i.e. during that build -- so the frozen state is replayed by every
instance: the first draw of a fresh instance is the same number every time. For a gambling or
contest bundle that is a fairness bug (a fixed, and so predictable, "random" outcome). The fix is
`random.SystemRandom()`: it keeps no state and reads the OS entropy source on every call
(`wasi:random` in the component), so there is nothing to freeze.

This gate proves, over EVERY `bundles/python/*/src/app.py`:

1. **No frozen-state RNG in a game bundle.** A game bundle (the explicit list below, plus any
   bundle that imports the `economy` facade) has no `random.<fn>(...)` call other than
   `random.SystemRandom(...)` -- `random.choice`, `random.randint`, `random.Random()`,
   `random.seed` ... -- and no `from random import <fn>`.
2. **Every other module-level `random` user is classified.** A bundle that still uses it must be
   on the reasoned COSMETIC allowlist (flavor-text picks where no stake, winner or secret rides on
   the draw); a new bundle that uses module-level `random` must classify itself. The allowlist is
   exact: an entry that no longer uses module-level `random` (or became a game) fails, so it can
   only shrink.
3. **Denominators.** A scan that finds too few bundles, or no `SystemRandom` in the bundles known
   to need it, FAILS -- a mis-pointed glob must not pass vacuously.

Run: python3 -m pytest scripts/ci/tests/test_bundle_rng_gate.py
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLES_DIR = REPO_ROOT / "bundles" / "python"

#: Bundles whose randomness decides an outcome somebody relies on being fair: points won or lost,
#: a winner, a roll/flip/spin result, a hidden answer, a chance pick. (Plus any bundle that imports
#: the `economy` facade -- see `_is_game`.)
GAME_BUNDLES = frozenset(
    {
        "jackpot", "gamble", "slots", "roulette", "heist", "duel", "steal", "defuse",
        "blackjack", "dice", "roll", "coinflip", "rps", "wheel", "fish", "guess",
        "prediction", "lottery", "bingo", "keywordgiveaway", "raffle", "winner",
        "hangman", "trivia", "scramble", "eightball", "choose", "customcommand",
    }
)  # fmt: skip

#: Bundles that still draw from module-level `random`, reasoned: a flavor-text pick (a joke, a
#: greeting, a hug template) -- nothing is staked, won or hidden on it. The cost of the frozen
#: state there is variety (a fresh instance repeats its first pick), not fairness; the fleet-wide
#: follow-up is a `random.seed()` at the SDK entry point, verified against a real instance.
COSMETIC_REASON = "flavor-text pick: no stake, winner or secret rides on the draw"
COSMETIC = frozenset(
    {
        "advice", "affirmation", "boop", "catfact", "compliment", "dadjoke", "dance", "dogfact",
        "fact", "fortune", "hello", "highfive", "horoscope", "hug", "insult", "joke", "kiss",
        "mood", "motivate", "pat", "pickup", "poke", "roast", "slap", "social", "truthordare",
        "vibecheck", "waddle", "wave", "wouldyourather", "yomama",
    }
)  # fmt: skip


@dataclass(slots=True)
class RngUse:
    """How one `app.py` touches the `random` module."""

    frozen: list[str] = field(
        default_factory=list
    )  # module-level-state draws (the bug)
    system_random: int = 0  # `random.SystemRandom(...)` constructions (the fix)
    imports_economy: bool = False


def analyse(source: str) -> RngUse:
    """Classify every use of the `random` module and the `economy` facade in one source."""
    tree = ast.parse(source)
    use = RngUse()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "random"
        ):
            if node.func.attr == "SystemRandom":
                use.system_random += 1
            else:
                use.frozen.append(f"random.{node.func.attr}(...)@{node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "random":
                for alias in node.names:
                    if alias.name != "SystemRandom":
                        use.frozen.append(
                            f"from random import {alias.name}@{node.lineno}"
                        )
            elif node.module == "waddle_sdk" and any(
                a.name == "economy" for a in node.names
            ):
                use.imports_economy = True
    return use


def _scan() -> dict[str, RngUse]:
    """`{bundle: RngUse}` for every bundle `app.py`."""
    return {
        path.parent.parent.name: analyse(path.read_text(encoding="utf-8"))
        for path in sorted(BUNDLES_DIR.glob("*/src/app.py"))
    }


SCAN = _scan()


def _is_game(name: str) -> bool:
    return name in GAME_BUNDLES or SCAN[name].imports_economy


GAMES = {name for name in SCAN if _is_game(name)}


def test_denominator_the_scan_found_the_bundles() -> None:
    """A mis-pointed glob must fail loudly, not pass vacuously."""
    assert len(SCAN) >= 100, f"expected >=100 bundles, found {len(SCAN)}"
    assert GAME_BUNDLES <= set(SCAN), sorted(GAME_BUNDLES - set(SCAN))
    needing_the_fix = {
        "jackpot", "gamble", "slots", "roulette", "heist", "duel", "steal", "defuse", "blackjack",
        "dice", "roll", "coinflip", "rps", "wheel", "fish", "guess", "lottery", "bingo",
        "keywordgiveaway", "raffle", "winner", "hangman", "trivia", "scramble", "eightball",
        "choose", "customcommand",
    }  # fmt: skip
    missing = sorted(n for n in needing_the_fix if SCAN[n].system_random == 0)
    assert not missing, f"no random.SystemRandom() seen in {missing} (scanner broken?)"


@pytest.mark.parametrize("name", sorted(GAMES))
def test_a_game_bundle_never_uses_the_module_level_random_state(name: str) -> None:
    frozen = SCAN[name].frozen
    assert not frozen, (
        f"{name} draws from the module-level `random` generator ({frozen}). componentize-py "
        "freezes its import-time Mersenne Twister state into the build snapshot, so every "
        "instance replays one stream -- a predictable outcome. Use a module-level "
        "`_RNG = random.SystemRandom()` (OS entropy via wasi:random) and call `_RNG.<fn>(...)`."
    )


def test_every_other_module_level_random_user_is_classified() -> None:
    unclassified = sorted(
        name
        for name, use in SCAN.items()
        if use.frozen and name not in GAMES | COSMETIC
    )
    assert not unclassified, (
        f"{unclassified} use module-level `random` but are neither a game bundle nor on the "
        "COSMETIC allowlist: if the draw decides anything somebody relies on being fair, add the "
        "bundle to GAME_BUNDLES and switch it to random.SystemRandom(); if it is only a "
        f"flavor-text pick, add it to COSMETIC ({COSMETIC_REASON})"
    )


def test_the_cosmetic_allowlist_is_exact_and_can_only_shrink() -> None:
    stale = sorted(n for n in COSMETIC if n not in SCAN or not SCAN[n].frozen)
    assert not stale, (
        f"COSMETIC entries that no longer use module-level random: {stale}"
    )
    promoted = sorted(COSMETIC & GAMES)
    assert not promoted, (
        f"COSMETIC entries that are game bundles (convert them): {promoted}"
    )


# -- the gate can fail ------------------------------------------------------------------------


def test_the_gate_flags_module_level_random_calls() -> None:
    src = "import random\n\ndef roll():\n    return random.randint(1, 6)\n"
    assert analyse(src).frozen == ["random.randint(...)@4"]


def test_the_gate_flags_a_seeded_or_module_level_random_instance() -> None:
    src = "import random\n_rng = random.Random()\nrandom.seed(1)\n"
    assert analyse(src).frozen == ["random.Random(...)@2", "random.seed(...)@3"]


def test_the_gate_flags_from_random_imports() -> None:
    src = "from random import choice, SystemRandom\n"
    assert analyse(src).frozen == ["from random import choice@1"]


def test_the_gate_passes_systemrandom() -> None:
    src = "import random\n_RNG = random.SystemRandom()\n\ndef f():\n    return _RNG.randint(1, 6)\n"
    use = analyse(src)
    assert use.frozen == [] and use.system_random == 1


def test_the_gate_detects_an_economy_bundle() -> None:
    assert analyse("from waddle_sdk import economy, log\n").imports_economy
    assert not analyse("from waddle_sdk import log\n").imports_economy
