from pathlib import Path
from hermie.config import Config

def test_config_precedence(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('port = 9000\nlanguages = ["en", "zh"]\ndeny_words = ["Codename"]\n')
    monkeypatch.setenv("HERMIE_PORT", "9100")
    cfg = Config.load(tmp_path / "config.toml", mode="observe")
    assert cfg.port == 9100 and cfg.languages == ("en", "zh") and cfg.deny_words == ("Codename",) and cfg.mode == "observe"

def test_paths_live_under_data_dir(tmp_path):
    cfg = Config(data_dir=tmp_path)
    assert cfg.mapping_path == tmp_path / "mapping.json" and cfg.outbound_dir == tmp_path / "outbound"
    assert cfg.allowed_path == tmp_path / "allowed.jsonl"


import pytest


@pytest.mark.parametrize("kw", [{"mode": "enforc"}, {"images": "maybe"}, {"judge_threshold": 1.5}, {"port": 0}, {"cache_mb": -1}])
def test_invalid_values_raise(kw):
    with pytest.raises(ValueError, match=next(iter(kw))):
        Config(**kw)


def test_invalid_env_names_the_variable(monkeypatch):
    monkeypatch.setenv("HERMIE_BODIES", "flase")
    with pytest.raises(ValueError, match="HERMIE_BODIES"):
        Config.load()
    monkeypatch.delenv("HERMIE_BODIES")
    monkeypatch.setenv("HERMIE_PORT", "abc")
    with pytest.raises(ValueError, match="HERMIE_PORT"):
        Config.load()


def test_empty_data_dir_env_is_unset(monkeypatch):
    monkeypatch.setenv("HERMIE_DATA_DIR", "")
    assert Config.load().data_dir == Config().data_dir


def test_local_model_defaults_and_validation(tmp_path):
    c = Config(data_dir=tmp_path)
    assert c.local_model is None and c.local_timeout_s == 60.0 and c.local_max_chars == 12000
    assert Config(data_dir=tmp_path, local_model="ollama:qwen3:8b").local_model == "ollama:qwen3:8b"
    for bad in ("qwen3", "laya:/x", "ollama:"):
        with pytest.raises(ValueError):
            Config(data_dir=tmp_path, local_model=bad)
    with pytest.raises(ValueError):
        Config(data_dir=tmp_path, local_timeout_s=-1)
    with pytest.raises(ValueError):
        Config(data_dir=tmp_path, local_max_chars=0)


def test_local_model_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMIE_LOCAL_MODEL", "ollama:qwen3:8b")
    monkeypatch.setenv("HERMIE_LOCAL_MAX_CHARS", "500")
    c = Config.load(data_dir=tmp_path)
    assert c.local_model == "ollama:qwen3:8b" and c.local_max_chars == 500
    monkeypatch.setenv("HERMIE_LOCAL_MODEL", "")
    assert Config.load(data_dir=tmp_path).local_model is None


@pytest.mark.parametrize("body", ['port = "8787"', 'bodies = "false"', "judge_threshold = true", "cache_mb = 1.5",
                                  "languages = [1]", "host = 1", "judge = 3", "judge_timeout_s = nan", "data_dir = 5"])
def test_config_file_values_are_type_checked(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(body + "\n")
    with pytest.raises(ValueError, match="invalid config"):
        Config.load(p)


def test_config_file_accepts_an_int_for_a_float(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("judge_timeout_s = 30\njudge_threshold = 1\n")
    assert Config.load(p).judge_timeout_s == 30


def test_anthropic_upstream_defaults_and_validates(tmp_path, monkeypatch):
    assert Config(data_dir=tmp_path).anthropic_upstream == "https://api.anthropic.com"
    monkeypatch.setenv("HERMIE_ANTHROPIC_UPSTREAM", "http://127.0.0.1:9999")
    (tmp_path / "empty.toml").write_text("")
    assert Config.load(tmp_path / "empty.toml").anthropic_upstream == "http://127.0.0.1:9999"
    with pytest.raises(ValueError):
        Config(data_dir=tmp_path, anthropic_upstream="ftp://x")
