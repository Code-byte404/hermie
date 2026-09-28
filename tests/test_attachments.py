"""Attachments: paths dropped into the input box become material for the task (read in the main process, so files
outside the workspace work too; the content goes through the privacy gate like any other task text)."""
from pathlib import Path

from hermie.attachments import find_paths, load_material


def test_find_paths_recognises_existing_absolute_paths(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hi")
    text = f"summarize {f} please"
    assert find_paths(text) == [f]


def test_find_paths_handles_escaped_spaces_quotes_and_tilde(tmp_path, monkeypatch):
    d = tmp_path / "my docs"
    d.mkdir()
    f = d / "a b.md"
    f.write_text("x")
    monkeypatch.setenv("HOME", str(tmp_path))
    escaped = str(f).replace(" ", "\\ ")
    quoted = f"'{f}'"
    tilde = "~/my\\ docs/a\\ b.md"
    assert find_paths(f"read {escaped}") == [f]
    assert find_paths(f"read {quoted}") == [f]
    assert find_paths(f"read {tilde}") == [f]


def test_find_paths_ignores_missing_paths_and_dedupes(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    text = f"{f} and /nonexistent/zzz.txt and {f} again"
    assert find_paths(text) == [f]


def test_load_material_reads_text_files_with_headers(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello world")
    m = load_material([f], max_file_chars=1000, max_total_chars=5000)
    assert f"[File: {f}]" in m.text and "hello world" in m.text
    assert m.summary == [f"{f} (11 B)"]
    assert m.notes == []


def test_load_material_skips_binary_and_truncates_large(tmp_path):
    b = tmp_path / "img.png"
    b.write_bytes(b"\x89PNG\x00\x00binary")
    big = tmp_path / "big.txt"
    big.write_text("x" * 500)
    m = load_material([b, big], max_file_chars=100, max_total_chars=5000)
    assert "binary" not in m.text
    assert any("img.png" in n and "binary" in n for n in m.notes)
    assert m.text.split("]\n", 1)[1] == "x" * 100
    assert any("big.txt" in n and "truncated" in n for n in m.notes)


def test_load_material_directory_gives_tree_only(tmp_path):
    d = tmp_path / "proj"
    (d / "src").mkdir(parents=True)
    (d / "src" / "main.py").write_text("SECRET_CONTENT = 1")
    (d / "README.md").write_text("readme body")
    (d / ".git").mkdir()
    (d / ".git" / "HEAD").write_text("ref")
    (d / "node_modules").mkdir()
    (d / "node_modules" / "x.js").write_text("js")
    m = load_material([d], max_file_chars=1000, max_total_chars=5000)
    assert f"[Directory: {d}]" in m.text
    assert "src/main.py" in m.text and "README.md" in m.text
    assert "SECRET_CONTENT" not in m.text and "readme body" not in m.text
    assert ".git" not in m.text and "node_modules" not in m.text
    assert m.summary == [f"{d}/ (2 files)"]


def test_load_material_refuses_credential_files(tmp_path):
    k = tmp_path / "id_rsa"
    k.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc")
    m = load_material([k], max_file_chars=1000, max_total_chars=5000, deny_names=("id_rsa", "*.pem"))
    assert "PRIVATE KEY" not in m.text
    assert any("id_rsa" in n and "credential" in n for n in m.notes)


def test_load_material_total_cap_stops_further_files(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a" * 80)
    b.write_text("b" * 80)
    m = load_material([a, b], max_file_chars=1000, max_total_chars=100)
    assert "a" * 80 in m.text
    assert "b" * 80 not in m.text
    assert any("b.txt" in n and "limit" in n for n in m.notes)


def test_find_paths_ignores_root_and_home():
    assert find_paths("/") == []
    assert find_paths("/help") == []
    assert find_paths("~") == []
    assert find_paths(f"list {Path.home()}") == []
