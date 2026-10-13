"""Repo-wide guard: no Python source may parse XML with an XXE-unsafe parser.

The stdlib XML parsers (``xml.etree.ElementTree`` parse APIs, ``xml.dom.minidom``,
``xml.dom.pulldom``, ``xml.sax``, ``xml.parsers.expat``, ``xmlrpc``) and ``lxml``
are not safe against entity-expansion / external-entity attacks on untrusted
input.  Untrusted XML (CalDAV responses, PubSubHubbub bodies, ...) must go
through ``defusedxml``.  Importing the stdlib ElementTree for *non-parsing* names
(``Element``, ``SubElement``, ``register_namespace``, ``ParseError``) is allowed.

A guard that cannot fail is not a guard: ``TestScanner`` proves the scanner flags
known-bad imports, and the repo scan asserts it examined files.
"""

from __future__ import annotations

import ast
import os
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".worktrees",
        ".claude",
        "target",
        "__pycache__",
        "site-packages",
        "dist",
        "build",
    }
)

# Modules that must never be imported (any submodule, any form).
_BANNED_MODULE_PREFIXES = (
    "xml.dom",
    "xml.sax",
    "xml.parsers",
    "xml.etree.cElementTree",
    "xmlrpc",
    "lxml",
)

# Names that give access to a parser when imported from xml.etree[.ElementTree].
_ETREE_PARSING_NAMES = frozenset(
    {
        "ElementTree",
        "XML",
        "XMLID",
        "fromstring",
        "fromstringlist",
        "parse",
        "iterparse",
        "XMLParser",
        "XMLPullParser",
        "XMLTreeBuilder",
    }
)


def unsafe_xml_imports(source: str) -> list[tuple[int, str]]:
    """Return (lineno, description) for each XXE-unsafe XML import in ``source``."""
    findings: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if (
                    name.startswith(_BANNED_MODULE_PREFIXES)
                    or name == "xml.etree"
                    or name.startswith("xml.etree.ElementTree")
                ):
                    findings.append((node.lineno, f"import {name}"))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module = node.module
            if module.startswith(_BANNED_MODULE_PREFIXES):
                findings.append((node.lineno, f"from {module} import ..."))
            elif module == "xml" and any(a.name in {"dom", "sax", "parsers"} for a in node.names):
                findings.append((node.lineno, "from xml import dom/sax/parsers"))
            elif module in {"xml.etree", "xml.etree.ElementTree"}:
                bad = sorted(a.name for a in node.names if a.name in _ETREE_PARSING_NAMES)
                if bad:
                    findings.append((node.lineno, f"from {module} import {', '.join(bad)}"))
    return findings


def _python_files(root: Path) -> list[Path]:
    """Collect .py files under ``root``, skipping vendored / build / worktree dirs."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        files.extend(Path(dirpath, f) for f in filenames if f.endswith(".py"))
    return files


class TestScanner:
    """The scanner flags unsafe imports and accepts the safe forms."""

    def test_flags_stdlib_elementtree_module_import(self) -> None:
        """Module-level stdlib ElementTree imports are flagged."""
        assert unsafe_xml_imports("from xml.etree import ElementTree as ET")
        assert unsafe_xml_imports("import xml.etree.ElementTree as ET")
        assert unsafe_xml_imports("import xml.etree.ElementTree")

    def test_flags_stdlib_parse_names(self) -> None:
        """Importing stdlib ElementTree parse functions by name is flagged."""
        assert unsafe_xml_imports("from xml.etree.ElementTree import fromstring")
        assert unsafe_xml_imports("from xml.etree.ElementTree import Element, parse")

    def test_flags_other_unsafe_parsers(self) -> None:
        """minidom, sax, expat, xmlrpc and lxml imports are flagged."""
        for src in (
            "import xml.dom.minidom",
            "from xml.dom import minidom",
            "from xml import sax",
            "import xml.sax.handler",
            "from xml.parsers import expat",
            "import xmlrpc.client",
            "from lxml import etree",
            "import lxml.etree",
        ):
            assert unsafe_xml_imports(src), src

    def test_allows_safe_forms(self) -> None:
        """Safe imports (defusedxml, non-parsing ElementTree names) are accepted."""
        for src in (
            "from defusedxml import ElementTree",
            "import defusedxml.minidom",
            "from defusedxml.common import DefusedXmlException",
            "from xml.etree.ElementTree import Element",
            "from xml.etree.ElementTree import Element, SubElement, register_namespace",
            "import json, xmlschema_not_a_parser",
        ):
            assert unsafe_xml_imports(src) == [], src


def test_no_unsafe_xml_parsing_in_repo() -> None:
    """No tracked Python file imports an XXE-unsafe XML parser."""
    files = _python_files(REPO_ROOT)
    assert files, f"scanned 0 Python files under {REPO_ROOT}: scanner is pointed at the wrong root"
    violations: list[str] = []
    scanned = 0
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
            # Cheap prefilter: every flagged import names "xml" (incl. lxml, xmlrpc).
            # Files without it cannot violate; they still count as examined.
            if "xml" in source:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")  # legacy files: invalid escapes etc.
                    findings = unsafe_xml_imports(source)
            else:
                findings = []
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        scanned += 1
        violations.extend(
            f"{path.relative_to(REPO_ROOT)}:{lineno}: {desc}" for lineno, desc in findings
        )
    assert scanned > 0, "every candidate file failed to parse"
    assert not violations, (
        f"XXE-unsafe XML parser imports found ({scanned} files scanned); "
        "use defusedxml instead:\n" + "\n".join(violations)
    )
