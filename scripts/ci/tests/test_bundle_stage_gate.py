"""Gate: process-only host capabilities must never be reachable from a bundle's `dispatch`.

The real host splits the pipeline into two stages that serve DIFFERENT capabilities:

* process stage (`core/svc_process`, the bundle's `transform`): `economy`, `identity`
  (`resolve_actor` / `resolve_mention`), `reputation`, `ai`;
* action stage (`core/svc_action`, the bundle's `dispatch`): `relay` -- and NO handler for the
  four above (`core/svc_action/src/capabilities.rs`: only log/http/kv/db/flags are wired).

A stage-blind fake accepts every call from either entry point, so a bundle that settled money in
`dispatch` passed every host-native test and then failed on a real host (steal, defuse, jackpot,
top, prediction and lottery all did). This gate proves, over EVERY `bundles/python/*/src/app.py`:

1. **Static**: no function reachable from `dispatch` makes a process-only host call, and no
   function reachable from `transform` calls `relay` (the process stage hard-denies it). The call
   graph is conservative -- any reference to a module-level function or any method call by name
   counts as an edge -- so a helper cannot hide a call.
2. **Fakes enforce it**: every bundle that makes such a call anywhere has a test suite built on
   `waddle_sdk.testing.StageGuard` (or the equivalent `stage_violations` recorder), so a bundle
   cannot regress behind a stage-blind fake.
3. **Denominators**: a scan that finds too few bundles, or no host call in a bundle known to make
   them, FAILS -- a mis-pointed glob must not pass vacuously.

Run: python3 -m pytest scripts/ci/tests/test_bundle_stage_gate.py
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLES_DIR = REPO_ROOT / "bundles" / "python"

#: Host-call attributes per SDK facade module the process stage alone serves (`None` = any).
PROCESS_ONLY_CALLS: dict[str, frozenset[str] | None] = {
    "economy": frozenset({"balance", "wager", "transfer", "max_bet", "leaderboard"}),
    "identity": frozenset({"resolve_actor", "resolve_mention"}),
    "reputation": None,
    "ai": frozenset({"generate"}),
}
ACTION_ONLY_CALLS: dict[str, frozenset[str] | None] = {"relay": None}

#: Bundles that settle money / resolve identities and so MUST show host calls in `transform`.
KNOWN_PROCESS_BUNDLES = frozenset(
    {
        "steal",
        "defuse",
        "jackpot",
        "top",
        "prediction",
        "lottery",
        "blackjack",
        "chatguard",
    }
)

#: Markers proving a bundle's tests use a stage-faithful fake.
STAGE_FAKE_MARKERS = ("StageGuard", "stage_violations")

#: Method names that collide with builtin container/str methods (`payload.get(...)` is a dict
#: lookup, not a call into the module's own `_Ledger.get`). An attribute call with one of these
#: names is a call-graph edge only when its receiver is a local known to hold a module class.
CONTAINER_METHODS = frozenset(
    {
        "get", "set", "add", "pop", "items", "keys", "values", "append", "extend", "update",
        "setdefault", "copy", "clear", "join", "split", "strip", "lower", "upper", "format",
        "startswith", "endswith", "count", "index", "remove", "insert", "sort", "encode",
        "decode", "read", "write", "delete", "increment",
    }
)  # fmt: skip

#: The ops of the SDK facades that touch the host only for the stage split (kv/log are served by
#: both stages and are deliberately not listed).


@dataclass(slots=True)
class Analysis:
    """The call structure of one `app.py`: who references whom, and who calls which host op."""

    edges: dict[str, set[str]] = field(default_factory=dict)
    process_sites: dict[str, list[str]] = field(default_factory=dict)
    action_sites: dict[str, list[str]] = field(default_factory=dict)

    def reachable(self, entry: str) -> set[str]:
        """Every function name reachable from `entry` (inclusive; empty if it is not defined)."""
        seen: set[str] = set()
        stack = [entry] if entry in self.edges else []
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.add(name)
            stack.extend(self.edges.get(name, ()))
        return seen

    def sites_in(self, names: set[str], table: dict[str, list[str]]) -> list[str]:
        """`function: call` strings for every recorded site inside `names`."""
        return sorted(f"{fn}: {site}" for fn in names for site in table.get(fn, ()))


def _imported_aliases(tree: ast.Module) -> tuple[dict[str, str], dict[str, str]]:
    """`(module aliases, direct host names)`: local name -> facade module / `module.attr`."""
    modules: dict[str, str] = {}
    direct: dict[str, str] = {}
    facades = {**PROCESS_ONLY_CALLS, **ACTION_ONLY_CALLS}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "waddle_sdk":
                for alias in node.names:
                    if alias.name in facades:
                        modules[alias.asname or alias.name] = alias.name
            elif node.module and node.module.startswith("waddle_sdk."):
                facade = node.module.split(".", 1)[1]
                if facade in facades:
                    for alias in node.names:
                        direct[alias.asname or alias.name] = f"{facade}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("waddle_sdk.") and alias.asname:
                    facade = alias.name.split(".", 1)[1]
                    if facade in facades:
                        modules[alias.asname] = facade
    return modules, direct


def _typed_locals(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, classes: dict[str, set[str]]
) -> dict[str, set[str]]:
    """`{local: its module class's methods}` for params annotated, or locals built, from a class."""
    typed: dict[str, set[str]] = {}
    for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs):
        note = arg.annotation
        name = note.id if isinstance(note, ast.Name) else None
        if (
            name is None
            and isinstance(note, ast.Constant)
            and isinstance(note.value, str)
        ):
            name = note.value
        if name in classes:
            typed[arg.arg] = classes[name]
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
            if isinstance(value, ast.Await):
                value = value.value
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in classes
            ):
                typed[target.id] = classes[value.func.id]
    return typed


def _local_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Parameters and assigned names of `fn` (a local variable is not a function reference)."""
    args = fn.args
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    names.update(a.arg for a in (args.vararg, args.kwarg) if a is not None)
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def _host_call(
    node: ast.Call, modules: dict[str, str], direct: dict[str, str]
) -> tuple[str, str] | None:
    """`(facade, op)` if `node` calls an SDK facade op (`economy.wager(...)` / a direct import)."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        facade = modules.get(func.value.id)
        if facade is not None:
            return facade, func.attr
    if isinstance(func, ast.Name) and func.id in direct:
        facade, _, op = direct[func.id].partition(".")
        return facade, op
    return None


def analyse(source: str) -> Analysis:
    """Build the conservative call graph and host-call sites of one bundle `app.py` source."""
    tree = ast.parse(source)
    modules, direct = _imported_aliases(tree)
    funcs: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    classes: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(node.name, []).append(node)
        elif isinstance(node, ast.ClassDef):
            classes[node.name] = {
                m.name
                for m in node.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    result = Analysis(edges={name: set() for name in funcs})
    for name, nodes in funcs.items():
        for fn in nodes:
            local = _local_names(fn)
            typed = _typed_locals(fn, classes)
            for node in ast.walk(fn):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    if node.id in funcs and node.id not in local and node.id != name:
                        result.edges[name].add(node.id)
                elif isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute) and node.func.attr in funcs:
                        attr, base = node.func.attr, node.func.value
                        receiver = base.id if isinstance(base, ast.Name) else None
                        if attr in CONTAINER_METHODS and receiver != "self":
                            known = typed.get(receiver or "")
                            linked = known is not None and attr in known
                        else:
                            linked = True
                        if linked and attr != name:
                            result.edges[name].add(attr)
                    hit = _host_call(node, modules, direct)
                    if hit is None:
                        continue
                    facade, op = hit
                    for table, spec in (
                        (result.process_sites, PROCESS_ONLY_CALLS),
                        (result.action_sites, ACTION_ONLY_CALLS),
                    ):
                        allowed = spec.get(facade, False)
                        if allowed is False:
                            continue
                        if allowed is None or op in allowed:
                            table.setdefault(name, []).append(
                                f"{facade}.{op}@{node.lineno}"
                            )
    return result


def _scan() -> dict[str, Analysis]:
    """`{bundle: Analysis}` for every bundle `app.py` that defines `dispatch`."""
    found: dict[str, Analysis] = {}
    for path in sorted(BUNDLES_DIR.glob("*/src/app.py")):
        analysis = analyse(path.read_text(encoding="utf-8"))
        if "dispatch" in analysis.edges:
            found[path.parent.parent.name] = analysis
    return found


SCAN = _scan()
WITH_PROCESS_CALLS = {name for name, a in SCAN.items() if a.process_sites}
#: Bundles that move money / resolve identities / touch reputation: their fakes must be stage-
#: faithful. (`ai` bundles are covered by the static reachability check above.)
WITH_MONEY_OR_IDENTITY_CALLS = {
    name
    for name, a in SCAN.items()
    if any(
        not site.startswith("ai.")
        for sites in a.process_sites.values()
        for site in sites
    )
}


def test_denominator_the_scan_found_the_bundles() -> None:
    """A mis-pointed glob must fail loudly, not pass vacuously."""
    assert len(SCAN) >= 100, (
        f"expected >=100 bundles with a dispatch, found {len(SCAN)}"
    )
    assert KNOWN_PROCESS_BUNDLES <= set(SCAN), sorted(KNOWN_PROCESS_BUNDLES - set(SCAN))
    assert KNOWN_PROCESS_BUNDLES <= WITH_MONEY_OR_IDENTITY_CALLS
    assert {"ask", "translate"} <= WITH_PROCESS_CALLS, (
        "the ai.generate bundles must be seen"
    )


def test_denominator_the_known_bundles_show_their_host_calls_in_transform() -> None:
    """The bundles that move money must be seen calling economy/identity from `transform`."""
    for name in sorted(KNOWN_PROCESS_BUNDLES):
        analysis = SCAN[name]
        sites = analysis.sites_in(
            analysis.reachable("transform"), analysis.process_sites
        )
        assert sites, (
            f"{name}: no process-only host call reachable from transform (scan broken?)"
        )


@pytest.mark.parametrize("name", sorted(SCAN))
def test_no_process_only_capability_is_reachable_from_dispatch(name: str) -> None:
    analysis = SCAN[name]
    bad = analysis.sites_in(analysis.reachable("dispatch"), analysis.process_sites)
    assert not bad, (
        f"{name}: dispatch (the ACTION stage; svc_action has no economy/identity/reputation/ai "
        f"handler) reaches {bad} -- move the call into transform (process stage) and have "
        "dispatch only relay the finished reply"
    )


@pytest.mark.parametrize("name", sorted(SCAN))
def test_relay_is_not_reachable_from_transform(name: str) -> None:
    analysis = SCAN[name]
    if "transform" not in analysis.edges:
        pytest.skip("no transform")
    bad = analysis.sites_in(analysis.reachable("transform"), analysis.action_sites)
    assert not bad, (
        f"{name}: transform (the PROCESS stage hard-denies relay) reaches {bad} -- relay only "
        "from dispatch; carry a failure marker in the reply event instead"
    )


@pytest.mark.parametrize("name", sorted(WITH_MONEY_OR_IDENTITY_CALLS))
def test_bundles_with_economy_or_identity_calls_test_against_stage_faithful_fakes(
    name: str,
) -> None:
    """The bundle's tests must fail if a capability is called from the wrong stage."""
    tests = sorted((BUNDLES_DIR / name / "tests").glob("*.py"))
    assert tests, f"{name}: makes process-only host calls but has no tests/"
    text = "\n".join(path.read_text(encoding="utf-8") for path in tests)
    assert any(marker in text for marker in STAGE_FAKE_MARKERS), (
        f"{name}: its test fakes do not enforce the stage split -- build them on "
        "waddle_sdk.testing.StageGuard (process_only / action_only / two_stage) and assert "
        "GUARD.assert_clean() in the host fixture"
    )


# -- the gate can fail ------------------------------------------------------------------------


BAD_DISPATCH = """
from waddle_sdk import economy, identity, relay

async def _helper(actor):
    return await economy.balance(actor)

async def transform(event):
    return event

async def dispatch(envelope, config, *, http_client):
    actor = await identity.resolve_actor()
    await _helper(actor)
    await relay.push("twitch", {})
"""

GOOD_SPLIT = """
from waddle_sdk import economy, identity, relay

async def _settle(event):
    return await economy.balance(await identity.resolve_actor())

async def transform(event):
    return await _settle(event)

async def dispatch(envelope, config, *, http_client):
    await relay.push("twitch", {})
"""

METHOD_HIDDEN = """
from waddle_sdk import economy

class _Game:
    async def pay(self):
        await economy.wager("u", 1, 2)

async def dispatch(envelope, config, *, http_client):
    await _Game().pay()
"""

BAD_RELAY_IN_TRANSFORM = """
from waddle_sdk import relay

async def transform(event):
    await relay.push("twitch", {})

async def dispatch(envelope, config, *, http_client):
    return None
"""

LOCAL_NAME_IS_NOT_A_CALL = """
from waddle_sdk import economy

async def user_uuid():
    return await economy.balance("u")

async def _pseudonym(community, user_uuid):
    return f"{community}{user_uuid}"

async def dispatch(envelope, config, *, http_client):
    await _pseudonym("c", "u")
"""

DIRECT_IMPORT = """
from waddle_sdk.economy import wager

async def dispatch(envelope, config, *, http_client):
    await wager("u", 1, 2)
"""


def test_the_gate_flags_a_dispatch_that_reaches_economy_or_identity() -> None:
    a = analyse(BAD_DISPATCH)
    assert a.sites_in(a.reachable("dispatch"), a.process_sites) == [
        "_helper: economy.balance@5",
        "dispatch: identity.resolve_actor@11",
    ]


def test_the_gate_passes_the_correct_split() -> None:
    a = analyse(GOOD_SPLIT)
    assert a.sites_in(a.reachable("dispatch"), a.process_sites) == []
    assert a.sites_in(a.reachable("transform"), a.process_sites) != []
    assert a.sites_in(a.reachable("transform"), a.action_sites) == []


def test_the_gate_sees_through_a_method_call() -> None:
    a = analyse(METHOD_HIDDEN)
    assert a.sites_in(a.reachable("dispatch"), a.process_sites) == [
        "pay: economy.wager@6"
    ]


def test_the_gate_flags_relay_reachable_from_transform() -> None:
    a = analyse(BAD_RELAY_IN_TRANSFORM)
    assert a.sites_in(a.reachable("transform"), a.action_sites) == [
        "transform: relay.push@5"
    ]


def test_a_local_variable_named_like_a_function_is_not_an_edge() -> None:
    a = analyse(LOCAL_NAME_IS_NOT_A_CALL)
    assert a.sites_in(a.reachable("dispatch"), a.process_sites) == []


def test_the_gate_flags_a_direct_import_call() -> None:
    a = analyse(DIRECT_IMPORT)
    assert a.sites_in(a.reachable("dispatch"), a.process_sites) == [
        "dispatch: economy.wager@5"
    ]
