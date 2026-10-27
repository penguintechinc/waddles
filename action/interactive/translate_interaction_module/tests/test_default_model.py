"""Regression guard: default WaddleAI model must be Google Gemma, never PRC-origin (Qwen)."""
import importlib.util
import pathlib

import pytest

CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config.py"


def _load_config(name: str):
    """Load config.py fresh by path so the env at call time determines the default."""
    spec = importlib.util.spec_from_file_location(name, CONFIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Config


def test_default_model_is_gemma_not_qwen(monkeypatch: pytest.MonkeyPatch):
    """Default is gemma4:e2b (supply-chain: no PRC-origin models)."""
    monkeypatch.delenv("WADDLEAI_MODEL", raising=False)
    model = _load_config("translate_config_default").WADDLEAI_MODEL
    assert model == "gemma4:e2b"
    assert "qwen" not in model.lower()


def test_model_still_env_overridable(monkeypatch: pytest.MonkeyPatch):
    """Only the default changed; WADDLEAI_MODEL stays env-overridable."""
    monkeypatch.setenv("WADDLEAI_MODEL", "gemma4:e4b")
    assert _load_config("translate_config_override").WADDLEAI_MODEL == "gemma4:e4b"
