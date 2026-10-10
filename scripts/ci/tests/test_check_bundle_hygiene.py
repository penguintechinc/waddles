"""Self-tests for the bundle-hygiene CI gates (scripts/ci/check-bundle-*.py).

A gate that cannot fail is not a gate (critical-rules.md Verification
Integrity), so every rule here is proven against a PLANTED violation in a
throwaway `bundles/` tree -- exit code non-zero and the finding named -- and
every safe shape the real tree uses is proven NOT to fire. The real repo tree
is also run end to end and must pass with a non-zero denominator.

Each script is exercised through its CLI (`--root <tmp>`), exactly as the
`bundle-hygiene` workflow invokes it.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CI_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CI_DIR.parent.parent
SOURCE_SCRIPT = CI_DIR / "check-bundle-source-hygiene.py"
MANIFEST_SCRIPT = CI_DIR / "check-bundle-manifest-permissions.py"

V2_MANIFEST = """\
schema_version: 2
app_id: waddles.core.example.demo
permissions:
{permissions}
routes_to: []
"""


def run(script: Path, *args: str, root: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Runs one gate script via its CLI against `root` (default: the real repo) and captures output."""
    cmd = [sys.executable, str(script), *args]
    if root is not None:
        cmd += ["--root", str(root)]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def make_bundle_src(root: Path, body: str, *, rel: str = "bundles/python/demo/src/app.py") -> Path:
    """Writes a bundle source file (dedented `body`) into a throwaway tree and returns its path."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def make_manifest(root: Path, content: str, *, name: str = "demo") -> Path:
    """Writes a `hub-manifest.yaml` for bundle `name` into a throwaway tree and returns its path."""
    path = root / "bundles" / "python" / name / "hub-manifest.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


# --- log-pii ---------------------------------------------------------------------------------

PII_VIOLATIONS = {
    "rest_kwarg": 'log.info("x.matched", platform=event.platform, rest=rest)',
    "message_kwarg_renamed": 'log.info("x.matched", detail=message)',
    "fstring_username": 'log.info(f"x.matched for {username}")',
    "fstring_at_target": 'log.info(f"x.matched @{target}")',
    "percent_positional": 'log.info("x.matched %s", message)',
    "event_actor": 'log.info("x.matched", who=event.actor)',
    "payload_text_get": 'log.debug("x.matched", body=event.payload.get("text"))',
    "payload_text_subscript": 'log.debug("x.matched", body=event.payload["text"])',
    "whole_payload": 'log.debug("x.matched", data=event.payload)',
    "whole_event": 'log.debug("x.matched", ev=event)',
    "method_on_tainted": 'log.info("x.matched", cleaned=rest.strip().lower())',
    "strict_key_opaque_value": 'log.info("x.matched", username=compute_value())',
    "target_token": 'log.info("x.matched", who=target_token)',
    "stdlib_logger_receiver": 'logger.warning("x.matched", mention=mention)',
    "self_log_receiver": 'self.log.error("x.matched", target=target)',
    "print_leak": "print(rest)",
    "kwargs_splat": 'log.info("x.matched", **{"user": rest})',
}


@pytest.mark.parametrize("snippet", PII_VIOLATIONS.values(), ids=PII_VIOLATIONS.keys())
def test_log_pii_planted_violation_fails(tmp_path: Path, snippet: str) -> None:
    """Every known raw-input leak shape must make the gate exit non-zero and name the file."""
    make_bundle_src(tmp_path, f"def handler(event, rest, message, username, target, mention, target_token):\n    {snippet}\n")
    proc = run(SOURCE_SCRIPT, "--check", "log-pii", root=tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "[log-pii]" in proc.stderr
    assert "bundles/python/demo/src/app.py:2" in proc.stderr
    assert "findings=1 -> FAIL" in proc.stdout


def test_log_pii_safe_fields_pass(tmp_path: Path) -> None:
    """The structural fields real bundles log (community/op/context/counts/exc type) must not fire."""
    make_bundle_src(tmp_path, '''
        def handler(event, payload, options, exc, community, op, target_ms, spec, reply):
            log.info("x.matched", community=community, op=op, context="active")
            log.info("x.matched", platform=event.platform, shape="targeted")
            log.info("x.matched", command=payload["command"], action=reply["action"])
            log.info("x.matched", command=spec.name, option_count=len(options))
            log.debug("x.fail", error_type=type(exc).__name__, error=str(exc))
            log.info("x.timer", target_ms=target_ms)
            log.info("x.channel", channel=event.payload.get("channel_id"))
            log.info("x.len", rest=len(event.payload.get("text") or ""))
            log.info("x.flag", has_target=target is not None)
        ''')
    proc = run(SOURCE_SCRIPT, "--check", "log-pii", root=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "log_calls_examined=9" in proc.stdout
    assert "findings=0 -> PASS" in proc.stdout


def test_log_pii_pragma_with_reason_suppresses_and_is_reported(tmp_path: Path) -> None:
    """`# pii-ok: <reason>` suppresses one call, and the suppression is listed in the summary."""
    make_bundle_src(tmp_path, '''
        def handler(rest):
            log.info("x.matched", rest=rest)  # pii-ok: rest here is a server-generated id
        ''')
    proc = run(SOURCE_SCRIPT, "--check", "log-pii", root=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "suppressed=1" in proc.stdout
    assert "server-generated id" in proc.stdout


def test_log_pii_pragma_without_reason_does_not_suppress(tmp_path: Path) -> None:
    """A bare `# pii-ok:` with no justification must NOT silence the gate."""
    make_bundle_src(tmp_path, '''
        def handler(rest):
            log.info("x.matched", rest=rest)  # pii-ok:
        ''')
    assert run(SOURCE_SCRIPT, "--check", "log-pii", root=tmp_path).returncode == 1


def test_log_pii_multiline_call_pragma_on_any_line(tmp_path: Path) -> None:
    """A pragma on any physical line of a multi-line log call applies to the whole call."""
    make_bundle_src(tmp_path, '''
        def handler(rest):
            log.info(
                "x.matched",
                rest=rest,  # pii-ok: opaque id
            )
        ''')
    assert run(SOURCE_SCRIPT, "--check", "log-pii", root=tmp_path).returncode == 0


# --- silent-except ---------------------------------------------------------------------------

SILENT_VIOLATIONS = {
    "except_exception_pass": "try:\n        int(x)\n    except Exception:\n        pass",
    "bare_except_pass": "try:\n        int(x)\n    except:\n        pass",
    "except_exception_return_none": "try:\n        return int(x)\n    except Exception:\n        return None",
    "except_exception_bare_return": "try:\n        int(x)\n    except Exception:\n        return",
    "except_exception_return_false": "try:\n        return int(x)\n    except Exception as exc:\n        return False",
    "except_exception_return_empty_dict": "try:\n        return int(x)\n    except Exception:\n        return {}",
    "tuple_with_exception_continue": "for _ in range(3):\n        try:\n            int(x)\n        except (ValueError, Exception):\n            continue",
    "except_exception_assign_default": "count = 5\n    try:\n        count = int(x)\n    except Exception:\n        count = 0",
    "except_exception_ellipsis": "try:\n        int(x)\n    except Exception:\n        ...",
    "except_baseexception_break": "while True:\n        try:\n            int(x)\n        except BaseException:\n            break",
    "contextlib_suppress_exception": "with contextlib.suppress(Exception):\n        int(x)",
    "bare_suppress_exception": "with suppress(ValueError, Exception):\n        int(x)",
}


@pytest.mark.parametrize("body", SILENT_VIOLATIONS.values(), ids=SILENT_VIOLATIONS.keys())
def test_silent_except_planted_violation_fails(tmp_path: Path, body: str) -> None:
    """Every broad swallow-to-default shape must make the gate exit non-zero and name the rule."""
    make_bundle_src(tmp_path, f"def handler(x):\n    {body}\n")
    proc = run(SOURCE_SCRIPT, "--check", "silent-except", root=tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "[silent-except]" in proc.stderr
    assert "findings=1 -> FAIL" in proc.stdout


def test_silent_except_expected_control_flow_and_handled_errors_pass(tmp_path: Path) -> None:
    """Narrow handlers, logged handlers, re-raises and reply-building handlers are not silent swallows."""
    make_bundle_src(tmp_path, '''
        def handler(x):
            try:
                parsed = parse(x)
            except CommandUsageError:
                parsed = None
            for _ in range(3):
                try:
                    return update(x)
                except db.ConflictError:
                    continue
            try:
                import wit_world
            except ImportError:
                return "free"
            try:
                return int(x)
            except Exception as exc:
                log.error("x.failed", error=str(exc))
                return None
            try:
                return int(x)
            except Exception:
                raise
            try:
                return int(x)
            except Exception as exc:
                return usage_reply(exc)
            try:
                return int(x)
            except Exception:
                cleanup()
                return None
        ''')
    proc = run(SOURCE_SCRIPT, "--check", "silent-except", root=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "except_handlers_examined=7" in proc.stdout


def test_silent_except_pragma_with_reason_suppresses(tmp_path: Path) -> None:
    """`# silent-ok: <reason>` on the except line suppresses one handler and is reported."""
    make_bundle_src(tmp_path, '''
        def handler(x):
            try:
                int(x)
            except Exception:  # silent-ok: best-effort cache warm, failure is harmless
                pass
        ''')
    proc = run(SOURCE_SCRIPT, "--check", "silent-except", root=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "suppressed=1" in proc.stdout


# --- denominator / scoping (shared by both source checks) ------------------------------------

@pytest.mark.parametrize("check", ["log-pii", "silent-except"])
def test_source_checks_empty_tree_is_a_failure_not_a_pass(tmp_path: Path, check: str) -> None:
    """A scan root with no bundle sources must FAIL (zero denominator), never report clean."""
    (tmp_path / "bundles").mkdir()
    proc = run(SOURCE_SCRIPT, "--check", check, root=tmp_path)
    assert proc.returncode == 1
    assert "zero denominator" in proc.stderr


@pytest.mark.parametrize("check", ["log-pii", "silent-except"])
def test_source_checks_missing_bundles_dir_fails(tmp_path: Path, check: str) -> None:
    """A root with no `bundles/` directory at all must FAIL, not pass."""
    assert run(SOURCE_SCRIPT, "--check", check, root=tmp_path).returncode == 1


def test_source_check_zero_log_calls_is_a_failure(tmp_path: Path) -> None:
    """Sources present but zero log calls examined is still a zero denominator for log-pii."""
    make_bundle_src(tmp_path, "def handler():\n    return 1\n")
    proc = run(SOURCE_SCRIPT, "--check", "log-pii", root=tmp_path)
    assert proc.returncode == 1
    assert "log_calls_examined=0" in proc.stderr


def test_source_checks_ignore_tests_and_non_src_dirs(tmp_path: Path) -> None:
    """Violations in tests/, test_*.py, or outside a `src/` dir are not shipped code and must not fire."""
    make_bundle_src(tmp_path, 'def ok(community):\n    log.info("x", community=community)\n    try:\n        pass\n    except ValueError:\n        pass\n')
    bad = 'def bad(rest):\n    log.info("x", rest=rest)\n    try:\n        pass\n    except Exception:\n        pass\n'
    make_bundle_src(tmp_path, bad, rel="bundles/python/demo/tests/test_app.py")
    make_bundle_src(tmp_path, bad, rel="bundles/python/demo/src/test_helpers.py")
    make_bundle_src(tmp_path, bad, rel="bundles/python/demo/scripts/tool.py")
    make_bundle_src(tmp_path, bad, rel="bundles/build_tool.py")
    proc = run(SOURCE_SCRIPT, root=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "files_scanned=1" in proc.stdout


def test_source_checks_syntax_error_fails(tmp_path: Path) -> None:
    """An unparseable bundle source file must FAIL the gate (it cannot be proven clean)."""
    make_bundle_src(tmp_path, "def broken(:\n")
    proc = run(SOURCE_SCRIPT, root=tmp_path)
    assert proc.returncode == 1
    assert "cannot parse" in proc.stderr


# --- manifest permissions --------------------------------------------------------------------

def permissions_block(*lines: str) -> str:
    """Builds a minimal manifest whose `permissions:` list is the given raw YAML lines."""
    return V2_MANIFEST.format(permissions="\n".join(lines))


def test_manifest_bare_string_permissions_fail(tmp_path: Path) -> None:
    """The legacy `permissions: [flags.read]` shape (the regression this gate exists for) must fail."""
    make_manifest(tmp_path, permissions_block("  - flags.read"))
    proc = run(MANIFEST_SCRIPT, root=tmp_path)
    assert proc.returncode == 1
    assert "legacy bare string 'flags.read'" in proc.stderr
    assert "violations=1 -> FAIL" in proc.stdout


def test_manifest_inline_bare_list_fails(tmp_path: Path) -> None:
    """Flow-style `permissions: [storage.kv, flags.read]` is the same defect and must fail per entry."""
    make_manifest(tmp_path, "schema_version: 2\npermissions: [storage.kv, flags.read]\n")
    proc = run(MANIFEST_SCRIPT, root=tmp_path)
    assert proc.returncode == 1
    assert "violations=2 -> FAIL" in proc.stdout


def test_manifest_mixed_shapes_fail(tmp_path: Path) -> None:
    """One bare string among valid V2 entries still fails -- only the bare entry is reported."""
    make_manifest(tmp_path, permissions_block(
        "  - id: flags.read", '    justification: "gates the command"', "  - storage.kv"))
    proc = run(MANIFEST_SCRIPT, root=tmp_path)
    assert proc.returncode == 1
    assert "permissions[1]" in proc.stderr
    assert "violations=1 -> FAIL" in proc.stdout


def test_manifest_valid_v2_and_explicit_empty_pass(tmp_path: Path) -> None:
    """V2 entries and the explicit `permissions: []` form both pass; counts are printed."""
    make_manifest(tmp_path, permissions_block("  - id: flags.read", '    justification: "gates the command"'), name="a")
    make_manifest(tmp_path, "schema_version: 2\npermissions: []\n", name="b")
    proc = run(MANIFEST_SCRIPT, root=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "manifests_examined=2 permission_entries_examined=1 violations=0 -> PASS" in proc.stdout


@pytest.mark.parametrize(
    "content,needle",
    [
        ("schema_version: 2\n", "no `permissions:` key"),
        ("schema_version: 2\npermissions: flags.read\n", "must be a list"),
        ("schema_version: 2\npermissions:\n  - id: flags.read\n", "missing a non-empty `justification`"),
        ('schema_version: 2\npermissions:\n  - id: flags.read\n    justification: ""\n', "missing a non-empty `justification`"),
        ('schema_version: 2\npermissions:\n  - justification: "why"\n', "missing a non-empty string `id`"),
        ("schema_version: 2\npermissions:\n  - id: flags.read\n    justification: " + "x" * 281 + "\n", "justification is 281 chars"),
        ("schema_version: 2\npermissions:\n  - 42\n", "must be a mapping"),
        ("permissions: [unterminated\n", "cannot parse manifest"),
        ("- just\n- a list\n", "not a YAML mapping"),
    ],
    ids=["missing_key", "scalar_permissions", "no_justification", "empty_justification",
         "no_id", "oversized_justification", "int_entry", "invalid_yaml", "non_mapping_doc"],
)
def test_manifest_malformed_permissions_fail(tmp_path: Path, content: str, needle: str) -> None:
    """Every structural defect (missing/blank fields, wrong types, bad YAML) fails with a specific message."""
    make_manifest(tmp_path, content)
    proc = run(MANIFEST_SCRIPT, root=tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert needle in proc.stderr


def test_manifest_zero_manifests_is_a_failure(tmp_path: Path) -> None:
    """No manifests under bundles/ must FAIL (zero denominator), never report clean."""
    (tmp_path / "bundles").mkdir()
    proc = run(MANIFEST_SCRIPT, root=tmp_path)
    assert proc.returncode == 1
    assert "zero hub-manifest.yaml found" in proc.stderr


# --- the real tree ---------------------------------------------------------------------------

def test_real_tree_passes_all_three_gates_with_nonzero_denominators() -> None:
    """The committed tree is clean, and the gates actually examined something (not a vacuous pass)."""
    source = run(SOURCE_SCRIPT)
    assert source.returncode == 0, source.stdout + source.stderr
    for token in ("files_scanned=", "log_calls_examined=", "except_handlers_examined="):
        assert token in source.stdout
        assert f"{token}0 " not in source.stdout
    manifest = run(MANIFEST_SCRIPT)
    assert manifest.returncode == 0, manifest.stdout + manifest.stderr
    assert "manifests_examined=0 " not in manifest.stdout
