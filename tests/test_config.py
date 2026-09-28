"""update_env: replaces in place, keeps comments and order, appends missing keys."""
from hermie.config import update_env


def test_update_env_replaces_in_place_and_appends(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# models\nWORKER_MODEL=qwen3.8:27b-mlx\nJUDGE_MODEL = gemma4:12b\n\n# other\nVOICE_OUTPUT=false\n")
    update_env(p, {"JUDGE_MODEL": "qwen3:8b", "VOICE_KEY": "f8"})
    assert p.read_text() == ("# models\nWORKER_MODEL=qwen3.8:27b-mlx\nJUDGE_MODEL=qwen3:8b\n\n# other\nVOICE_OUTPUT=false\n"
                             "VOICE_KEY=f8\n")


def test_update_env_creates_file(tmp_path):
    p = tmp_path / "sub" / ".env"
    p.parent.mkdir()
    update_env(p, {"VOICE_OUTPUT": "true"})
    assert p.read_text() == "VOICE_OUTPUT=true\n"


def test_attachment_limits_come_from_env(monkeypatch):
    from hermie.config import Settings
    monkeypatch.setenv("ATTACH_MAX_FILE_CHARS", "123")
    monkeypatch.setenv("ATTACH_MAX_TOTAL_CHARS", "456")
    s = Settings()
    assert s.attach_max_file_chars == 123 and s.attach_max_total_chars == 456


def _clear_cloud_env(monkeypatch):
    for k in ("CLOUD_PROVIDER", "CLOUD_API_KEY", "CLOUD_BASE_URL", "CLOUD_MODEL", "CLOUD_PLAN_MODEL",
              "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_PLAN_MODEL"):
        monkeypatch.delenv(k, raising=False)


def test_cloud_settings_default_to_deepseek_and_read_legacy_keys(monkeypatch):
    from hermie.config import Settings
    _clear_cloud_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-legacy")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash-lite")
    s = Settings()
    assert s.cloud_provider == "deepseek" and s.cloud_api_key == "sk-legacy"
    assert s.cloud_model == "deepseek-v4-flash-lite" and s.cloud_plan_model == "deepseek-v4-pro"
    assert s.cloud_label == "DeepSeek"


def test_cloud_settings_prefer_cloud_keys_over_legacy(monkeypatch):
    from hermie.config import Settings
    _clear_cloud_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-legacy")
    monkeypatch.setenv("CLOUD_PROVIDER", "anthropic")
    monkeypatch.setenv("CLOUD_API_KEY", "sk-ant")
    s = Settings()
    assert s.cloud_api_key == "sk-ant"
    assert s.cloud_model == "claude-sonnet-5" and s.cloud_plan_model == "claude-opus-5-5"
    assert s.cloud_label == "Anthropic"


def test_cloud_settings_openai_compatible_is_labelled_by_host(monkeypatch):
    from hermie.config import Settings
    _clear_cloud_env(monkeypatch)
    monkeypatch.setenv("CLOUD_PROVIDER", "openai-compatible")
    monkeypatch.setenv("CLOUD_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("CLOUD_MODEL", "qwen/qwen3-coder")
    s = Settings()
    assert s.cloud_label == "openrouter.ai" and s.cloud_model == "qwen/qwen3-coder" and s.cloud_plan_model == ""


def test_cloud_settings_reject_unknown_provider(monkeypatch):
    import pytest
    from hermie.config import Settings
    _clear_cloud_env(monkeypatch)
    monkeypatch.setenv("CLOUD_PROVIDER", "gemini")
    with pytest.raises(ValueError, match="CLOUD_PROVIDER"):
        Settings()
