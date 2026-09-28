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
