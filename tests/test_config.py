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
