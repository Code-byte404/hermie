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
