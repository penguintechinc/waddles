"""Guard: no connection ciphertext is opened or sealed outside the one versioned vault.

regression: M4 -- a reader that calls `decrypt_value(...)` directly bypasses the `seal_version`
dispatch (and therefore the AAD binding). Only the crypto module itself and the vault may touch
the raw primitives; everything else goes through `services.provider_connections.vault`.
"""

from __future__ import annotations

import ast
from pathlib import Path

HUB_API = Path(__file__).resolve().parents[1]
CRYPTO_MODULE = "services.platform_integrations_crypto"
GUARDED = frozenset({"decrypt_value", "encrypt_token"})
#: The only production modules allowed to call the raw primitives.
ALLOWED = frozenset(
    {"services/platform_integrations_crypto.py", "services/provider_connections/vault.py"}
)


def _production_files() -> list[Path]:
    """Every hub_api source file except tests (tests may exercise the primitives directly)."""
    return sorted(
        p
        for p in HUB_API.rglob("*.py")
        if "tests" not in p.relative_to(HUB_API).parts
        and ".venv" not in p.parts
        and "__pycache__" not in p.parts
    )


_MODULE_LEAF = CRYPTO_MODULE.rsplit(".", 1)[1]


def _is_crypto_module(module: str | None, level: int) -> bool:
    """Whether `from <dots><module> import ...` names the crypto module (absolute or relative)."""
    if module is None:
        return False
    return module == CRYPTO_MODULE or (level > 0 and module.split(".")[-1] == _MODULE_LEAF)


def violations_in(source: str) -> list[str]:
    """Return `name@line` for each raw-primitive use that resolves to the crypto module.

    Handles `from services.platform_integrations_crypto import x [as y]`, relative imports,
    `from services import platform_integrations_crypto [as c]`, `from . import ...`, and the dotted
    `import services.platform_integrations_crypto` spelling (matched on the full attribute chain).
    """
    tree = ast.parse(source)
    imported: set[str] = set()  # local names bound by `from <crypto> import decrypt_value [as x]`
    module_aliases: set[str] = set()  # dotted text that evaluates to the crypto module
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if _is_crypto_module(node.module, node.level):
                for alias in node.names:
                    if alias.name in GUARDED:
                        imported.add(alias.asname or alias.name)
                        found.append(f"import {alias.name}@{node.lineno}")
            else:
                for alias in node.names:
                    if alias.name == _MODULE_LEAF:
                        module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == CRYPTO_MODULE:
                    module_aliases.add(alias.asname or CRYPTO_MODULE)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in imported:
            found.append(f"{func.id}@{node.lineno}")
        elif (
            isinstance(func, ast.Attribute)
            and func.attr in GUARDED
            and ast.unparse(func.value) in module_aliases
        ):
            found.append(f"{func.attr}@{node.lineno}")
    return found


def no_direct_decrypt_value_outside_crypto_modules() -> tuple[int, dict[str, list[str]]]:
    """Scan production sources; return `(scanned_file_count, {file: violations})`."""
    files = _production_files()
    offenders: dict[str, list[str]] = {}
    for path in files:
        rel = path.relative_to(HUB_API).as_posix()
        if rel in ALLOWED:
            continue
        hits = violations_in(path.read_text(encoding="utf-8"))
        if hits:
            offenders[rel] = hits
    return len(files), offenders


def test_no_direct_decrypt_value_outside_crypto_modules() -> None:
    scanned_file_count, offenders = no_direct_decrypt_value_outside_crypto_modules()
    assert scanned_file_count > 0, "the guard scanned nothing -- wrong root"
    assert offenders == {}, f"route these through services.provider_connections.vault: {offenders}"


def test_the_guard_can_fail() -> None:
    """A stray direct call (any import spelling) is reported -- the gate is not vacuous."""
    assert violations_in(
        "from services.platform_integrations_crypto import decrypt_value\n"
        "def f(x):\n    return decrypt_value(x)\n"
    )
    assert violations_in(
        "from services import platform_integrations_crypto as c\n"
        "def f(x):\n    return c.encrypt_token(x)\n"
    )
    assert violations_in(
        "from services.platform_integrations_crypto import decrypt_value as d\nd('x')\n"
    )
    # the dotted spelling and relative imports are caught too
    assert violations_in(
        "import services.platform_integrations_crypto\n"
        "def f(x):\n    return services.platform_integrations_crypto.decrypt_value(x)\n"
    )
    assert violations_in(
        "import services.platform_integrations_crypto as pic\n"
        "def f(x):\n    return pic.encrypt_token(x)\n"
    )
    assert violations_in(
        "from .platform_integrations_crypto import decrypt_value\ndecrypt_value('x')\n"
    )
    assert violations_in(
        "from . import platform_integrations_crypto\n"
        "platform_integrations_crypto.decrypt_value('x')\n"
    )
    assert violations_in("def decrypt_value(x):\n    return x\ndecrypt_value('x')\n") == []


def test_the_allowed_modules_exist() -> None:
    for rel in ALLOWED:
        assert (HUB_API / rel).is_file(), rel
