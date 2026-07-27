"""One language setting feeds the whole voice stack (#1057).

Whisper, the TTS bridge, the HA assist pipeline and the gatekeeper's advertised
languages each carried their own hardcoded "de". This pins the single knob they
now share — and the convergence gap that made changing it a no-op on an already
existing pipeline.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("solaris_pd_lang", TEMPLATES / "solaris" / "post-deploy.py")


def test_system_language_defaults_to_german(pd, monkeypatch):
    monkeypatch.delenv("SOLARIS_LANGUAGE", raising=False)
    assert pd.system_language() == "de"


def test_system_language_follows_the_setting(pd, monkeypatch):
    monkeypatch.setenv("SOLARIS_LANGUAGE", "en")
    assert pd.system_language() == "en"


def test_whisper_language_override_still_wins(pd, monkeypatch):
    """The per-component override predates the shared setting and must keep
    beating it, otherwise an existing deployment changes behaviour silently."""
    monkeypatch.setenv("SOLARIS_LANGUAGE", "en")
    monkeypatch.setenv("WHISPER_LANGUAGE", "fr")
    assert pd.env("WHISPER_LANGUAGE", pd.system_language()) == "fr"
    monkeypatch.delenv("WHISPER_LANGUAGE")
    assert pd.env("WHISPER_LANGUAGE", pd.system_language()) == "en"


def test_template_and_variables_expose_one_language_knob():
    """The TTS bridge argument and the gatekeeper env must come from the same
    declared variable — a literal here is how the drift started."""
    tpl = (TEMPLATES / "solaris" / "template.yml").read_text()
    assert "{{SOLARIS_LANGUAGE}}" in tpl
    assert "\n    - de\n" not in tpl, "hardcoded TTS bridge language is back"

    variables = json.loads((TEMPLATES / "solaris" / "variables.json").read_text())
    assert variables["SOLARIS_LANGUAGE"]["default"] == "de"


def test_pipeline_language_is_not_hardcoded_anymore():
    """Guards the four `"language": "de"` literals the pipeline was built with,
    including the update branch that silently kept the old language."""
    src = (TEMPLATES / "solaris" / "post-deploy.py").read_text()
    for literal in (
        '"language": "de"',
        '"conversation_language": "de"',
        '"stt_language": "de"',
    ):
        assert literal not in src, f"hardcoded {literal} is back"
    assert 'upd["language"] = lang' in src, "existing pipelines must converge too"
