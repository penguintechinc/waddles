"""Standard `!<command>` subcommand/grammar parser for Waddles app bundles.

Every bundle command follows one shared grammar --
`!<command> [sub-module] [option] <input>` -- so bundle authors get
consistent parsing (`!lurk enable ai`, `!sr set youtube-labels ...`) instead
of each bundle hand-rolling its own `text.split()`. A bundle declares its
command name and (optionally) named sub-modules via `CommandSpec`;
`parse_command()` returns a structured `ParsedCommand` or raises
`CommandUsageError`, which a bundle maps straight to a usage reply -- see
`sdk/waddle-sdk/AUTHORING.md` for the full grammar reference and worked
examples. Deliberately dependency-free (no `kv`/WIT imports) -- pure text
in, structured data out -- see `waddle_sdk.sub_modules.SubModuleGate` for
the community-scoped enable/disable persistence layer built on top of this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The fixed verb vocabulary every bundle command grammar shares. `enable`/
#: `disable` take exactly one sub-module-name argument (see `parse_command`
#: and its `_finish_option` helper); every other verb takes free-text
#: `args` that the bundle itself parses further (e.g. `!sr set <key>
#: <value>` -- this parser stops at `args="<key> <value>"`).
VERBS: frozenset[str] = frozenset(
    {"set", "add", "sub", "enable", "disable", "remove", "delete", "list", "reset"}
)

_TOGGLE_VERBS: frozenset[str] = frozenset({"enable", "disable"})

_PLACEHOLDER_RE = re.compile(r"\$\((\w+)\)")


class CommandUsageError(ValueError):
    """Raised for any input that doesn't match a command's declared grammar.

    `str(exc)` is a ready-to-send usage/error message -- a bundle's
    `transform()` catches this and replies with it directly rather than
    silently dropping the command (AUTHORING.md's fail-loud rule: an
    unrecognized option is a reply, never a no-op).
    """


@dataclass(slots=True, frozen=True)
class CommandSpec:
    """A bundle's declared command name and (optional) named sub-modules.

    `name` excludes the leading `!`. `sub_modules` is the closed set of
    sub-module names this command recognizes (e.g. `!shoutout`'s
    `{"auto", "ai"}`) -- empty for a command with no sub-modules, in which
    case any sub-module-shaped token is an unrecognized-option error.
    Every declared sub-module starts disabled for every community
    (`waddle_sdk.sub_modules.SubModuleGate` enforces the default-OFF state;
    this spec only names them).
    """

    name: str
    sub_modules: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True, frozen=True)
class ParsedCommand:
    """The structured result of a successful `parse_command()` call.

    `option` is `None` for a bare `!<command>` (or `!<command> <sub-
    module>` alone) -- the bundle's own default behavior for that scope,
    which may be a real action (`!count`'s increment) or a usage reply;
    this parser doesn't decide which. `sub_module`/`args` are `None` when
    not present in the input.
    """

    command: str
    sub_module: str | None
    option: str | None
    args: str | None


def parse_command(text: str, spec: CommandSpec) -> ParsedCommand:
    """Parse one chat message against `spec`'s grammar.

    Recognizes a bare `!cmd`, `!cmd <option> <input>`, `!cmd <sub-module>
    <option> <input>`, and the enable/disable toggle shape `!cmd
    enable/disable <sub-module>` (equivalently `!cmd <sub-module>
    enable/disable`) -- per `!lurk enable ai`. Raises `CommandUsageError`
    for the wrong command name, an unrecognized option, an undeclared
    sub-module, or a malformed toggle; it never returns a best-guess
    partial parse.
    """
    stripped = text.strip()
    if not stripped:
        raise CommandUsageError("empty command text")

    head, _, rest = stripped.partition(" ")
    if head.lower() != f"!{spec.name}".lower():
        raise CommandUsageError(f"unrecognized command {head!r}, expected !{spec.name}")

    rest = rest.strip()
    if not rest:
        return ParsedCommand(command=spec.name, sub_module=None, option=None, args=None)

    tok1, _, tail = rest.partition(" ")
    tail_text = tail.strip() or None
    tok1_lower = tok1.lower()

    if tok1_lower in VERBS:
        return _finish_option(spec, sub_module=None, option=tok1_lower, tail=tail_text)

    if tok1_lower in spec.sub_modules:
        if tail_text is None:
            return ParsedCommand(command=spec.name, sub_module=tok1_lower, option=None, args=None)
        tok2, _, tail2 = tail_text.partition(" ")
        tail2_text = tail2.strip() or None
        tok2_lower = tok2.lower()
        if tok2_lower not in VERBS:
            raise CommandUsageError(f"!{spec.name} {tok1_lower}: unknown option {tok2!r}")
        return _finish_option(spec, sub_module=tok1_lower, option=tok2_lower, tail=tail2_text)

    raise CommandUsageError(f"!{spec.name}: unknown option or sub-module {tok1!r}")


def _finish_option(
    spec: CommandSpec, *, sub_module: str | None, option: str, tail: str | None
) -> ParsedCommand:
    """Resolve `option`'s own argument shape -- `enable`/`disable` take a bare sub-module name."""
    if option in _TOGGLE_VERBS:
        if sub_module is not None and tail is not None:
            raise CommandUsageError(
                f"!{spec.name} {sub_module} {option}: takes no further arguments"
            )
        target = sub_module or tail
        if target is None:
            raise CommandUsageError(f"!{spec.name} {option} requires a sub-module name")
        if " " in target:
            raise CommandUsageError(f"!{spec.name} {option}: takes exactly one sub-module name")
        target_lower = target.lower()
        if spec.sub_modules and target_lower not in spec.sub_modules:
            raise CommandUsageError(f"!{spec.name} {option}: unknown sub-module {target!r}")
        return ParsedCommand(command=spec.name, sub_module=target_lower, option=option, args=None)

    return ParsedCommand(command=spec.name, sub_module=sub_module, option=option, args=tail)


class MissingPlaceholderError(KeyError):
    """Raised by `substitute_placeholders()` for a `$(name)` with no supplied value.

    Fail loud -- a missing placeholder value is a bundle bug to surface
    immediately, never rendered as an empty string or left in the output
    literally (AUTHORING.md's no-silent-fallback rule).
    """


def extract_placeholders(template: str) -> list[str]:
    """Return every `$(name)` placeholder in `template`, first-seen order, deduplicated."""
    seen: dict[str, None] = {}
    for match in _PLACEHOLDER_RE.finditer(template):
        seen.setdefault(match.group(1), None)
    return list(seen)


def substitute_placeholders(template: str, values: dict[str, str]) -> str:
    """Replace every `$(name)` in `template` with `values[name]`.

    Raises `MissingPlaceholderError(name)` for any placeholder absent from
    `values` -- never silently drops or blanks it.
    """

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise MissingPlaceholderError(name)
        return values[name]

    return _PLACEHOLDER_RE.sub(_replace, template)
