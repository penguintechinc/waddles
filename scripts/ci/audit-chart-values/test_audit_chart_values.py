"""Tests for audit_chart_values.py: it must fail on dead values/helpers and on zero examined."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "audit_chart_values",
    os.path.join(os.path.dirname(__file__), "audit_chart_values.py"),
)
assert _SPEC and _SPEC.loader
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


def _chart(
    tmp: Path, values: str, templates: dict[str, str], overlay: str | None = None
) -> Path:
    """Write a minimal chart into tmp and return its path."""
    (tmp / "templates").mkdir(parents=True)
    (tmp / "Chart.yaml").write_text("apiVersion: v2\nname: t\nversion: 0.1.0\n")
    (tmp / "values.yaml").write_text(values)
    if overlay is not None:
        (tmp / "values-alpha.yaml").write_text(overlay)
    for name, body in templates.items():
        (tmp / "templates" / name).write_text(body)
    return tmp


_CLEAN_TPL = {
    "_helpers.tpl": '{{- define "t.name" -}}{{ .Values.name }}{{- end }}\n',
    "cm.yaml": (
        'name: {{ include "t.name" . }}\n'
        "{{- $db := .Values.db }}\n"
        "host: {{ $db.host }}\n"
        "res: {{- toYaml .Values.resources | nindent 2 }}\n"
        'x: {{ index .Values.map "a-b" }}\n'
    ),
}
_CLEAN_VALUES = (
    "name: n\ndb:\n  host: h\nresources:\n  limits:\n    cpu: 1\nmap:\n  a-b: 1\n"
)


def test_clean_chart_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Direct, aliased, wholesale (toYaml) and index references all count as used."""
    chart = _chart(tmp_path, _CLEAN_VALUES, _CLEAN_TPL)
    assert audit.main([str(chart)]) == 0
    assert "dead values: 0, unused helpers: 0" in capsys.readouterr().out


def test_dead_value_in_overlay_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A key no template reads -- even one only in an overlay -- fails the audit."""
    chart = _chart(
        tmp_path, _CLEAN_VALUES, _CLEAN_TPL, overlay="legacy:\n  replicas: 1\n"
    )
    assert audit.main([str(chart)]) == 1
    assert (
        "DEAD VALUE   legacy.replicas  (values-alpha.yaml)" in capsys.readouterr().out
    )
    chart2 = _chart(
        tmp_path / "b", _CLEAN_VALUES + "sharedEnv:\n  FOO: bar\n", _CLEAN_TPL
    )
    assert audit.main([str(chart2)]) == 1
    assert "DEAD VALUE   sharedEnv.FOO" in capsys.readouterr().out


def test_comment_reference_does_not_count(tmp_path: Path) -> None:
    """A `.Values.x` mention in a YAML or template comment is not a use."""
    tpl = dict(_CLEAN_TPL)
    tpl["c.yaml"] = "# uses .Values.ghost\n{{/* .Values.ghost too */}}\nk: v\n"
    chart = _chart(tmp_path, _CLEAN_VALUES + "ghost: 1\n", tpl)
    assert audit.main([str(chart)]) == 1


def test_unused_helper_fails_and_allowlist_needs_reason(tmp_path: Path) -> None:
    """A never-included define fails; allowlisting needs a '# reason'."""
    tpl = dict(_CLEAN_TPL)
    tpl["_helpers.tpl"] += '{{- define "t.dead" -}}x{{- end }}\n'
    chart = _chart(tmp_path, _CLEAN_VALUES, tpl)
    assert audit.main([str(chart)]) == 1
    allow = tmp_path / "allow.txt"
    allow.write_text("t.dead  # kept for an open PR\n")
    assert audit.main([str(chart), "--allowlist", str(allow)]) == 0
    allow.write_text("t.dead\n")
    with pytest.raises(SystemExit):
        audit.main([str(chart), "--allowlist", str(allow)])


def test_zero_examined_fails(tmp_path: Path) -> None:
    """An empty values file is a failure, never a vacuous pass."""
    chart = _chart(tmp_path, "", _CLEAN_TPL)
    assert audit.main([str(chart)]) == 1


def test_real_chart_is_clean() -> None:
    """The waddlebot chart itself must stay free of dead values and helpers."""
    root = Path(__file__).resolve().parents[3]
    rc = audit.main(
        [
            str(root / "k8s/helm/waddlebot"),
            "--allowlist",
            str(Path(__file__).with_name("allowlist.txt")),
        ]
    )
    assert rc == 0
