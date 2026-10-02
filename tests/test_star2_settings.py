"""Phase 1 — settings layer tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from Backend.star.config.dotenv import load_dotenv_file, parse_dotenv
from Backend.star.config.settings import PathsSettings, Settings, get_settings, reset_settings


@pytest.fixture(autouse=True)
def _clean_singleton():
    reset_settings()
    yield
    reset_settings()


def test_defaults_are_safe_and_dry_run() -> None:
    settings = Settings()
    assert settings.security.dry_run is True, "computer automation must start in dry-run"
    assert settings.security.allow_shell is False
    assert settings.security.deny_risk == "critical"
    assert settings.gateway.host == "0.0.0.0"
    assert settings.gateway.port == 8765
    assert settings.voice.languages == ("bn", "en")
    assert settings.voice.keep_transcripts is False, "transcripts are dropped unless opted in"
    assert settings.brain.max_plan_tasks >= 1


def test_env_overrides_are_applied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STAR_GATEWAY_PORT", "9911")
    monkeypatch.setenv("STAR_DRY_RUN", "false")
    monkeypatch.setenv("STAR_SAFETY_LEVEL", "paranoid")
    monkeypatch.setenv("STAR_LANGUAGES", "bn, en , hi")
    monkeypatch.setenv("STAR_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("STAR_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("STAR_VOICE_KEEP_TRANSCRIPTS", "yes")
    monkeypatch.setenv("STAR_TOOL_DENYLIST", "format_disk, rm_rf")

    settings = Settings.from_env(load_env_file=False)
    assert settings.gateway.port == 9911
    assert settings.security.dry_run is False
    assert settings.security.safety_level == "paranoid"
    assert settings.voice.languages == ("bn", "en", "hi")
    assert settings.voice.keep_transcripts is True
    assert settings.security.tool_denylist == ("format_disk", "rm_rf")
    assert settings.paths.logs_dir == tmp_path / "logs"


def test_bad_env_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAR_GATEWAY_PORT", "not-a-number")
    monkeypatch.setenv("STAR_MAX_PLAN_TASKS", "")
    settings = Settings.from_env(load_env_file=False)
    assert settings.gateway.port == 8765
    assert settings.brain.max_plan_tasks == 8


def test_public_dict_never_leaks_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSySECRETVALUE1234567890")
    settings = Settings.from_env(load_env_file=False)
    public = settings.public_dict()
    blob = str(public)
    assert "AIzaSySECRETVALUE1234567890" not in blob
    assert public["secrets"]["GEMINI_API_KEY"] == "<set>"
    assert public["secrets"]["OPENAI_API_KEY"] == "<unset>"


def test_paths_ensure_creates_runtime_dirs(tmp_path: Path) -> None:
    paths = PathsSettings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "data" / "workspace",
        logs_dir=tmp_path / "logs",
        browser_profile_root=tmp_path / "data" / "workspace" / "_profiles",
    )
    paths.ensure()
    assert paths.workspace_root.is_dir()
    assert paths.logs_dir.is_dir()
    assert paths.browser_profile_root.is_dir()


def test_get_settings_is_a_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAR_SESSION_ID", "unit-test")
    first = get_settings()
    second = get_settings()
    assert first is second
    assert first.session_id == "unit-test"
    monkeypatch.setenv("STAR_SESSION_ID", "reloaded")
    assert get_settings(reload=True).session_id == "reloaded"


def test_settings_are_immutable() -> None:
    settings = Settings()
    with pytest.raises(Exception):  # noqa: B017, PT011 — pydantic raises ValidationError/TypeError
        settings.session_id = "mutated"  # type: ignore[misc]


def test_parse_dotenv_handles_quotes_comments_and_export() -> None:
    text = """
    # comment
    STAR_GATEWAY_PORT=9000
    export STAR_OWNER="Nilanjan Nayak"
    STAR_TTS_VOICE='bn-IN-TanishaaNeural'
    BROKEN_LINE
    """
    parsed = parse_dotenv(text)
    assert parsed == {
        "STAR_GATEWAY_PORT": "9000",
        "STAR_OWNER": "Nilanjan Nayak",
        "STAR_TTS_VOICE": "bn-IN-TanishaaNeural",
    }


def test_load_dotenv_file_does_not_override_real_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("STAR_SESSION_ID=from-file\nSTAR_OWNER=from-file\n", encoding="utf-8")
    monkeypatch.setenv("STAR_OWNER", "from-env")
    applied = load_dotenv_file(env_file)
    assert applied == {"STAR_SESSION_ID": "from-file"}
    assert os.environ["STAR_OWNER"] == "from-env"
    assert load_dotenv_file(tmp_path / "missing.env") == {}
