"""Tests for flask_core.sanitization -- real bleach-backed XSS/filename/URL cleaning."""

import json

import pytest

from flask_core import sanitization as san


class TestSanitizeHtml:
    def test_empty_returns_empty(self):
        assert san.sanitize_html("") == ""
        assert san.sanitize_html(None) == ""

    def test_script_tag_stripped_safe_tags_kept(self):
        out = san.sanitize_html("<b>hi</b><script>alert(1)</script>")
        assert "<script" not in out
        assert "<b>hi</b>" in out

    def test_disallowed_attribute_removed(self):
        out = san.sanitize_html('<a href="https://x.io" onclick="evil()">l</a>')
        assert "onclick" not in out
        assert 'href="https://x.io"' in out

    def test_javascript_protocol_removed(self):
        out = san.sanitize_html('<a href="javascript:alert(1)">l</a>')
        assert "javascript:" not in out

    def test_custom_tags_attributes_protocols(self):
        out = san.sanitize_html(
            '<em class="k">x</em><b>y</b>',
            tags=["em"],
            attributes={"em": ["class"]},
            protocols=["https"],
        )
        assert out == '<em class="k">x</em>y'

    def test_strip_false_escapes_disallowed(self):
        out = san.sanitize_html("<script>x</script>", strip=False)
        assert "&lt;script&gt;" in out

    def test_bleach_failure_returns_empty(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("bleach down")

        monkeypatch.setattr(san, "clean", boom)
        assert san.sanitize_html("<b>x</b>") == ""


class TestSanitizeInput:
    def test_empty(self):
        assert san.sanitize_input("") == ""

    def test_strips_all_html_by_default(self):
        assert san.sanitize_input("<b>bold</b> text") == "bold text"

    def test_allow_html_keeps_safe_tags(self):
        assert san.sanitize_input("<b>bold</b><script>x</script>", allow_html=True) == "<b>bold</b>x"

    def test_failure_returns_empty(self, monkeypatch):
        def boom(*a, **k):
            raise ValueError("nope")

        monkeypatch.setattr(san, "clean", boom)
        assert san.sanitize_input("<b>x</b>") == ""


class TestSqlLikeAndWhitespace:
    def test_sql_like_escapes_wildcards_and_backslash(self):
        assert san.sanitize_sql_like("50%_off\\") == "50\\%\\_off\\\\"

    def test_sql_like_empty(self):
        assert san.sanitize_sql_like("") == ""

    def test_strip_whitespace_normalizes(self):
        assert san.strip_whitespace("  hello \t\n  world  ") == "hello world"

    def test_strip_whitespace_empty(self):
        assert san.strip_whitespace("") == ""


class TestSanitizeFilename:
    @pytest.mark.parametrize("raw", ["", None])
    def test_empty_is_unnamed(self, raw):
        assert san.sanitize_filename(raw) == "unnamed"

    def test_path_traversal_removed(self):
        assert san.sanitize_filename("../../etc/passwd") == "passwd"
        assert san.sanitize_filename("..\\..\\win\\sys.ini") == "sys.ini"

    def test_control_chars_and_invalid_chars(self):
        assert san.sanitize_filename("a\x00b<c>:d.txt") == "ab_c__d.txt"

    def test_only_dots_falls_back(self):
        assert san.sanitize_filename("...") == "unnamed"

    def test_truncate_preserves_extension(self):
        out = san.sanitize_filename("a" * 50 + ".png", max_length=10)
        assert len(out) == 10
        assert out.endswith(".png")

    def test_truncate_without_extension(self):
        assert san.sanitize_filename("b" * 50, max_length=8) == "b" * 8


class TestSanitizeUrl:
    def test_empty_is_none(self):
        assert san.sanitize_url("") is None

    @pytest.mark.parametrize(
        "url",
        ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "vbscript:x", "file:///etc/passwd"],
    )
    def test_dangerous_protocols_blocked(self, url):
        assert san.sanitize_url(url) is None

    def test_valid_http_https_pass_and_trimmed(self):
        assert san.sanitize_url("  https://example.com/x ") == "https://example.com/x"
        assert san.sanitize_url("http://example.com") == "http://example.com"

    def test_scheme_not_allowed(self):
        assert san.sanitize_url("ftp://example.com") is None

    def test_custom_allowed_schemes(self):
        assert san.sanitize_url("ftp://example.com", allowed_schemes=["ftp"]) == "ftp://example.com"
        assert san.sanitize_url("https://example.com", allowed_schemes=["ftp"]) is None


class TestJsonStringAndTruncate:
    def test_json_string_escapes_roundtrip(self):
        raw = 'a"b\\c\nd\re\tf\bg\fh'
        escaped = san.sanitize_json_string(raw)
        assert json.loads(f'"{escaped}"') == raw

    def test_json_string_empty(self):
        assert san.sanitize_json_string("") == ""

    def test_truncate_noop_when_short_or_empty(self):
        assert san.truncate_text("abc", 10) == "abc"
        assert san.truncate_text("", 3) == ""

    def test_truncate_with_suffix(self):
        assert san.truncate_text("abcdefghij", 6) == "abc..."

    def test_truncate_suffix_longer_than_max(self):
        assert san.truncate_text("abcdefghij", 2) == "ab..."


class TestValidators:
    def test_html_validator_modes(self):
        assert san.sanitized_html_validator("<b>x</b>") == "<b>x</b>"
        assert san.sanitized_html_validator("<b>x</b>", allow_html=False) == "x"

    def test_filename_validator(self):
        assert san.sanitized_filename_validator("../a.txt") == "a.txt"

    def test_url_validator_ok_and_reject(self):
        assert san.sanitized_url_validator("https://ok.io") == "https://ok.io"
        with pytest.raises(ValueError):
            san.sanitized_url_validator("javascript:alert(1)")
