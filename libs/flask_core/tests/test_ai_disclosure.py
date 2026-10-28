"""Tests for the EU AI Act Art.50 AI-generated disclosure helper."""

from flask_core.ai_disclosure import AI_DISCLOSURE, with_ai_disclosure


def test_appends_disclosure() -> None:
    """A bare reply gains the marker."""
    out = with_ai_disclosure("The answer is 42.")
    assert out == "The answer is 42." + AI_DISCLOSURE
    assert out.endswith("AI-generated")


def test_idempotent() -> None:
    """Already-disclosed text is not double-marked."""
    once = with_ai_disclosure("hi")
    assert with_ai_disclosure(once) == once
