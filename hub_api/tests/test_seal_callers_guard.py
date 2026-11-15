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


# ---- platform secrets (platform_vault, migration 0109) ----------------------------------------
#
# The platform-secret primitives live ONLY in the vault's platform functions: the AEAD, the KEK
# subkey derivation and the keyed MACs. Everything else (T3 bootstrap, the repository, rotation)
# calls `seal_platform_secret` / `open_platform_secret` / the MAC helpers, never the primitives.

VAULT = "services/provider_connections/vault.py"
#: The only top-level functions of the vault allowed to run the platform primitives.
PLATFORM_PRIMITIVE_FUNCTIONS = frozenset(
    {
        "seal_platform_secret",
        "open_platform_secret",
        "seal_escrow_object",
        "open_escrow_object",
        "_platform_key",
        "_platform_mac",
    }
)
#: Callables that ARE the platform primitives (AEAD, KEK-subkey derivation, keyed MAC).
PLATFORM_PRIMITIVES = frozenset({"AESGCM", "derive_key", "derive_kek_subkey"})
PLATFORM_HMAC_CALLS = frozenset({"new", "digest"})
#: Names that must never appear inside the platform seal/open functions: any of them is the legacy
#: path (no-AAD fallback, `seal_version=0` handling) or the wrong root (CREDENTIAL_ENCRYPTION_KEY).
LEGACY_NAMES = frozenset(
    {
        "crypto",
        "decrypt_value",
        "encrypt_token",
        "open_secret",
        "seal_secret",
        "open_classified",
        "legacy_fallback_allowed",
        "SEAL_LEGACY",
        "SEAL_AAD",
        "_KEY_ENV_MARKER",
    }
)
#: The reserved HKDF namespace of the platform-secret key family.
NAMESPACE_LITERAL = "waddles/platform-secrets"
NAMESPACE_NAMES = frozenset({"PLATFORM_INFO_NAMESPACE"})

#: Production modules that import `cryptography` or `hmac` today (ratchet baseline, 30 files). A NEW
#: module doing so fails `test_no_new_crypto_importers`: add it here only after review, and prefer
#: calling the vault. Shrink this set as modules move behind the vault; never grow it silently.
CRYPTO_IMPORT_BASELINE = frozenset(
    {
        "services/ai_routing/byok_crypto.py",
        "services/bot_crypto.py",
        "services/bundle_secret_crypto.py",
        "services/bundle_signing_service.py",
        "services/bundle_tool_transport.py",
        "services/envelope/aws_kms.py",
        "services/envelope/azure_key_vault.py",
        "services/envelope/crypto.py",
        "services/envelope/gcp_kms.py",
        "services/envelope/platform_kek.py",
        "services/envelope/service.py",
        "services/event_sync_connector_client.py",
        "services/github_sync_service.py",
        "services/install_state_binding.py",
        "services/marketplace_crypto.py",
        "services/marketplace_execution_service.py",
        "services/odoo_sync_service.py",
        "services/one_time_secret_crypto.py",
        "services/platform_integrations_crypto.py",
        "services/provider_connections/vault.py",
        "services/provider_connections/wix.py",
        "services/role_sync_inbound_service.py",
        "services/session_service.py",
        "services/sso_crypto.py",
        "services/sso_oidc.py",
        "services/sso_saml.py",
        "services/stream_dek_broker.py",
        "services/stripe_service.py",
        "services/suppression_keys.py",
        "services/youtube_install.py",
    }
)
CRYPTO_ROOTS = frozenset({"cryptography", "hmac"})


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    """Map every node to the name of the top-level function (or class method) containing it."""
    owner: dict[ast.AST, str] = {}
    for top in ast.iter_child_nodes(tree):
        if isinstance(top, ast.FunctionDef | ast.AsyncFunctionDef):
            for node in ast.walk(top):
                owner[node] = top.name
        elif isinstance(top, ast.ClassDef):
            for member in top.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    for node in ast.walk(member):
                        owner[node] = f"{top.name}.{member.name}"
    return owner


def _primitive_name(call: ast.Call) -> str | None:
    """The platform primitive a call invokes (`AESGCM`, `derive_key`, `hmac.new`...), if any."""
    func = call.func
    if isinstance(func, ast.Name) and func.id in PLATFORM_PRIMITIVES:
        return func.id
    if isinstance(func, ast.Attribute):
        if func.attr in PLATFORM_PRIMITIVES:
            return func.attr
        if func.attr in PLATFORM_HMAC_CALLS and ast.unparse(func.value) == "hmac":
            return f"hmac.{func.attr}"
    return None


def platform_primitive_violations(source: str) -> list[str]:
    """Return `name@line` for each platform primitive called outside the allowed functions.

    Flags `AESGCM(...)`, `<x>.derive_key(...)`, `derive_kek_subkey(...)` and `hmac.new/digest`.
    """
    tree = ast.parse(source)
    owner = _enclosing_functions(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _primitive_name(node)
        if name is not None and owner.get(node) not in PLATFORM_PRIMITIVE_FUNCTIONS:
            found.append(f"{name}@{node.lineno}")
    return found


def legacy_names_in(source: str, functions: frozenset[str]) -> list[str]:
    """Return `name@line` for each legacy-path / wrong-root name used inside `functions`."""
    tree = ast.parse(source)
    found: list[str] = []
    for top in ast.iter_child_nodes(tree):
        if isinstance(top, ast.FunctionDef) and top.name in functions:
            for node in ast.walk(top):
                if isinstance(node, ast.Name) and node.id in LEGACY_NAMES:
                    found.append(f"{node.id}@{node.lineno}")
                elif (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and "CREDENTIAL_ENCRYPTION_KEY" in node.value
                ):
                    found.append(f"CREDENTIAL_ENCRYPTION_KEY@{node.lineno}")
    return found


def namespace_references_in(source: str) -> list[str]:
    """Return `ref@line` for each use of the reserved platform-secret HKDF namespace."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant):
            value = node.value
            text = value.decode("latin-1") if isinstance(value, bytes) else value
            if isinstance(text, str) and NAMESPACE_LITERAL in text:
                found.append(f"literal@{node.lineno}")
        elif isinstance(node, ast.Name) and node.id in NAMESPACE_NAMES:
            found.append(f"{node.id}@{node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr in NAMESPACE_NAMES:
            found.append(f"{node.attr}@{node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            found.extend(
                f"import {a.name}@{node.lineno}" for a in node.names if a.name in NAMESPACE_NAMES
            )
    return found


def crypto_imports_in(source: str) -> list[str]:
    """Return `module@line` for each `cryptography` / `hmac` import, nested imports included."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend(
                f"{a.name}@{node.lineno}"
                for a in node.names
                if a.name.split(".")[0] in CRYPTO_ROOTS
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and node.module.split(".")[0] in CRYPTO_ROOTS
        ):
            found.append(f"{node.module}@{node.lineno}")
    return found


def test_platform_primitives_run_only_inside_the_platform_functions() -> None:
    """AESGCM / KEK-subkey derivation / hmac appear only in the vault's platform functions.

    So the legacy functions (and any later addition) cannot touch the platform key family.
    """
    source = (HUB_API / VAULT).read_text(encoding="utf-8")
    assert platform_primitive_violations(source) == []
    # denominator: the allowed functions really contain the primitives (the scan is not vacuous)
    tree = ast.parse(source)
    owner = _enclosing_functions(tree)
    used = {
        owner[n]
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and n in owner and _primitive_name(n) is not None
    }
    assert used == PLATFORM_PRIMITIVE_FUNCTIONS, used


def test_platform_seal_and_open_have_no_legacy_path_or_wrong_root() -> None:
    """The platform functions have no legacy escape hatch and no second root (review B3).

    They never reference the legacy crypto module, the no-AAD fallback, the `seal_version=0`
    constants or CREDENTIAL_ENCRYPTION_KEY.
    """
    source = (HUB_API / VAULT).read_text(encoding="utf-8")
    functions = frozenset({"seal_platform_secret", "open_platform_secret", "_platform_key"})
    assert legacy_names_in(source, functions) == []
    defined = {
        n.name for n in ast.iter_child_nodes(ast.parse(source)) if isinstance(n, ast.FunctionDef)
    }
    assert functions <= defined, "a platform function is missing (the scan would be vacuous)"


def test_only_the_vault_references_the_platform_key_namespace() -> None:
    """No other production module names the reserved HKDF namespace.

    So no other module can derive the key family that opens (or MACs) platform secrets.
    """
    files = _production_files()
    assert len(files) > 100
    offenders = {}
    for path in files:
        rel = path.relative_to(HUB_API).as_posix()
        if rel == VAULT:
            continue
        hits = namespace_references_in(path.read_text(encoding="utf-8"))
        if hits:
            offenders[rel] = hits
    assert offenders == {}, f"derive platform keys only through the vault: {offenders}"
    assert namespace_references_in(
        (HUB_API / VAULT).read_text(encoding="utf-8")
    ), "vault scan empty"


def test_no_new_crypto_importers() -> None:
    """Ratchet (review item 13): no module outside the baseline imports `cryptography` or `hmac`.

    New code calls the vault instead. A baseline file that no longer imports must be removed from
    the baseline, so the set only ever shrinks.
    """
    importers = {
        path.relative_to(HUB_API).as_posix()
        for path in _production_files()
        if crypto_imports_in(path.read_text(encoding="utf-8"))
    }
    assert len(importers) >= 25, "the scan found almost no importers -- wrong root"
    new = sorted(importers - CRYPTO_IMPORT_BASELINE)
    assert new == [], f"new crypto importers (use services.provider_connections.vault): {new}"
    stale = sorted(CRYPTO_IMPORT_BASELINE - importers)
    assert stale == [], f"stale baseline entries, remove them: {stale}"


def test_the_platform_guards_can_fail() -> None:
    """Each platform guard reports a synthetic violation -- none is vacuous."""
    assert platform_primitive_violations(
        "from cryptography.hazmat.primitives.ciphers.aead import AESGCM\n"
        "def stray(k, iv, ct):\n    return AESGCM(k).decrypt(iv, ct, None)\n"
    )
    assert platform_primitive_violations("def stray(kek):\n    return kek.derive_key(b'x')\n")
    assert platform_primitive_violations(
        "import hmac\ndef stray(k):\n    return hmac.new(k, b'm')\n"
    )
    assert (
        platform_primitive_violations("def _platform_mac(kek):\n    return kek.derive_key(b'x')\n")
        == []
    )
    assert legacy_names_in(
        "def open_platform_secret(x):\n    return crypto.decrypt_value(x)\n",
        frozenset({"open_platform_secret"}),
    )
    assert legacy_names_in(
        "def seal_platform_secret(x):\n    return os.getenv('CREDENTIAL_ENCRYPTION_KEY')\n",
        frozenset({"seal_platform_secret"}),
    )
    assert namespace_references_in("INFO = b'waddles/platform-secrets/v1'\n")
    assert namespace_references_in("from x import PLATFORM_INFO_NAMESPACE\n")
    assert namespace_references_in("y = vault.PLATFORM_INFO_NAMESPACE\n")
    assert namespace_references_in("INFO = b'waddles/tenant-dek/v1'\n") == []
    assert crypto_imports_in("def f():\n    import hmac\n")
    assert crypto_imports_in("from cryptography.fernet import Fernet\n")
    assert crypto_imports_in("import json\n") == []
