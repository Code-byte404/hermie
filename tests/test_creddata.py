"""The CredData benchmark code (evals/creddata): labels, hit parsing, the Hermie runner and the scorer, on small
fake datasets built here. The real dataset and the scanners are never needed."""
import random
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evals.creddata import labels  # noqa: E402

HEADER = ("Id,FileID,Domain,RepoName,FilePath,LineStart,LineEnd,GroundTruth,ValueStart,ValueEnd,"
          "CryptographyKey,PredefinedPattern,Category\n")


def write_meta(meta_dir: Path, repo: str, lines: list[str]) -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / f"{repo}.csv").write_text(HEADER + "".join(line + "\n" for line in lines))


# ---------------------------------------------------------------- labels

def test_load_meta_parses_rows(tmp_path):
    write_meta(tmp_path, "02dfa7ec", [
        "34024,61ed9af5,GitHub,02dfa7ec,data/02dfa7ec/test/61ed9af5.example,83,83,T,31,73,,,Secret:Token",
        "30,1d02852d,GitHub,02dfa7ec,data/02dfa7ec/src/1d02852d.c,475,475,F,,,,,Password",
        "31,1d02852d,GitHub,02dfa7ec,data/02dfa7ec/src/1d02852d.c,476,478,X,-1,-1,,,Password",
    ])
    rows = sorted(labels.load_meta(tmp_path), key=lambda r: r.id)
    assert [r.id for r in rows] == [30, 31, 34024]
    t = rows[2]
    assert (t.repo, t.file, t.line_start, t.line_end, t.label) == (
        "02dfa7ec", "data/02dfa7ec/test/61ed9af5.example", 83, 83, "T")
    assert (t.value_start, t.value_end) == (31, 73)
    assert t.categories == ("Secret", "Token")
    assert rows[0].value_start is None and rows[0].value_end is None
    assert rows[1].value_start is None and rows[1].value_end is None
    assert (rows[1].line_start, rows[1].line_end, rows[1].label) == (476, 478, "X")


def test_split_is_stable():
    expected = "test" if zlib.crc32(b"00408ef6") % 2 == 1 else "dev"
    assert labels.split_of("00408ef6") == expected == labels.split_of("00408ef6")
    rnd = random.Random(7)
    ids = [f"{rnd.getrandbits(32):08x}" for _ in range(200)]
    assert {labels.split_of(i) for i in ids} == {"dev", "test"}


def _row(categories):
    return labels.Row(1, "r", "data/r/a", 1, 1, "T", None, None, categories)


def test_scope():
    assert not labels.in_scope(_row(("UUID",)))
    assert not labels.in_scope(_row(("Nonce", "Salt")))
    assert labels.in_scope(_row(("Key", "UUID")))
    assert labels.in_scope(_row(("Password",)))


# ---------------------------------------------------------------- hits and parsers

from evals.creddata import hits  # noqa: E402


def fake_value(n: int = 16) -> str:
    rnd = random.Random()
    return "sk-test-" + "".join(rnd.choice("0123456789abcdef") for _ in range(n))


def make_file(root: Path, rel: str, content: str | bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    (p.write_bytes if isinstance(content, bytes) else p.write_text)(content)
    return p


def test_offsets_to_hit_single_and_multi_line():
    text = "a\nkey = sk-test-ab12\nmore\nxyz\n"
    s = text.index("sk-test")
    h = hits.offsets_to_hit("hermie", "data/r/a", text, s, s + 12, "SECRET")
    assert (h.line_start, h.line_end, h.col_start, h.col_end) == (2, 2, 6, 18)
    end = text.index("xyz") + 3
    h = hits.offsets_to_hit("hermie", "data/r/a", text, s, end, "SECRET")
    assert (h.line_start, h.line_end, h.col_start, h.col_end) == (2, 4, 6, 3)


def test_split_lines_crlf():
    assert hits.split_lines("a\r\nb") == ["a", "b"]


def test_read_text_invalid_utf8(tmp_path):
    p = make_file(tmp_path, "x.txt", b"ok\n\xff\xfe\n")
    text, bad = hits.read_text(p)
    assert bad is True and text.startswith("ok\n")
    assert hits.read_text(make_file(tmp_path, "y.txt", "fine\n"))[1] is False


def test_parse_gitleaks_recovers_columns(tmp_path):
    v = fake_value()
    line = 'github_token = "' + v + '"'
    make_file(tmp_path, "data/r1/src/a.py", "x = 1\n" + line + "\n")
    raw = [{"File": "data/r1/src/a.py", "StartLine": 2, "EndLine": 2, "StartColumn": 99, "EndColumn": 120,
            "RuleID": "generic-api-key", "Secret": v}]
    got, fallbacks = hits.parse_gitleaks(raw, tmp_path)
    assert got == [hits.Hit("gitleaks", "data/r1/src/a.py", 2, 2, 16, 16 + len(v), "generic-api-key")]
    assert fallbacks == 0
    raw[0]["Secret"] = "not-on-this-line"
    got, fallbacks = hits.parse_gitleaks(raw, tmp_path)
    assert got == [hits.Hit("gitleaks", "data/r1/src/a.py", 2, 2, None, None, "generic-api-key")]
    assert fallbacks == 1


def test_parse_trufflehog_jsonl(tmp_path):
    v = fake_value()
    make_file(tmp_path, "data/r1/src/b.env", "TOKEN=" + v + "\n")
    raw = ('{"SourceMetadata":{"Data":{"Filesystem":{"file":"data/r1/src/b.env","line":1}}},'
           '"DetectorName":"Github","Verified":false,"Raw":"' + v + '"}\n')
    got, fallbacks = hits.parse_trufflehog(raw, tmp_path)
    assert got == [hits.Hit("trufflehog", "data/r1/src/b.env", 1, 1, 6, 6 + len(v), "Github")]
    assert fallbacks == 0


def test_parse_detect_secrets(tmp_path):
    raw = {"results": {"data/r1/src/a.py": [{"type": "Secret Keyword", "filename": "data/r1/src/a.py",
                                              "hashed_secret": "00", "is_verified": False, "line_number": 3}]}}
    got, fallbacks = hits.parse_detect_secrets(raw, tmp_path)
    assert got == [hits.Hit("detect_secrets", "data/r1/src/a.py", 3, 3, None, None, "Secret Keyword")]
    assert fallbacks == 0


def test_parse_credsweeper(tmp_path):
    raw = [{"rule": "Token", "severity": "medium", "confidence": "moderate", "ml_probability": 0.9,
            "line_data_list": [{"line": "t = x", "line_num": 4, "path": "data/r1/src/a.py", "value": "x",
                                "value_start": 16, "value_end": 56},
                               {"line": "u = y", "line_num": 5, "path": "data/r1/src/a.py", "value": "y",
                                "value_start": 4, "value_end": 5}]}]
    got, fallbacks = hits.parse_credsweeper(raw, tmp_path)
    assert got == [hits.Hit("credsweeper", "data/r1/src/a.py", 4, 4, 16, 56, "Token"),
                   hits.Hit("credsweeper", "data/r1/src/a.py", 5, 5, 4, 5, "Token")]
    assert fallbacks == 0


def test_file_paths_normalized(tmp_path):
    raw = {"results": {str(tmp_path / "data/r1/a.py"): [{"type": "T", "line_number": 1}],
                       "./data/r1/b.py": [{"type": "T", "line_number": 1}]}}
    got, _ = hits.parse_detect_secrets(raw, tmp_path)
    assert sorted(h.file for h in got) == ["data/r1/a.py", "data/r1/b.py"]


def test_hits_jsonl_roundtrip(tmp_path):
    hs = [hits.Hit("hermie", "data/r/a", 1, 2, 3, None, "SECRET")]
    hits.write_hits(tmp_path / "h.jsonl", hs)
    hits.write_hits(tmp_path / "h.jsonl", hs)
    assert hits.read_hits(tmp_path / "h.jsonl") == hs + hs


# ---------------------------------------------------------------- Hermie runner

import json  # noqa: E402
import string  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from evals.creddata import run_hermie  # noqa: E402
from hermie.config import Config  # noqa: E402
from hermie.gate.gate import Gate  # noqa: E402
from hermie.gate.redact import MappingStore  # noqa: E402


def gate_factory(analyzer):
    return lambda d: Gate(Config(data_dir=d), analyzer=analyzer, store=MappingStore(d / "mapping.json"))


def random_alnum(n: int) -> str:
    rnd = random.Random()
    return "".join(rnd.choice(string.ascii_letters + string.digits) for _ in range(n))


def test_scan_file_secret_hit_columns(tmp_path, analyzer):
    v = random_alnum(32)
    make_file(tmp_path, "data/r/src/a.py", 'x = 1\napi_secret = "' + v + '"\n')
    got = run_hermie.scan_file(gate_factory(analyzer), tmp_path, "data/r/src/a.py", tmp_path / "tmp")
    assert hits.Hit("hermie", "data/r/src/a.py", 2, 2, 14, 46, "SECRET") in got


def test_scan_file_fresh_mapping_per_file(tmp_path, analyzer):
    word = "tulip" + str(random.Random().randrange(1000, 9999))
    make_file(tmp_path, "data/r/src/a.py", 'password = "' + word + '"\n')
    make_file(tmp_path, "data/r/src/b.md", "we planted " + word + " in the garden\n")
    f = gate_factory(analyzer)
    a = run_hermie.scan_file(f, tmp_path, "data/r/src/a.py", tmp_path / "tmp")
    assert any(h.kind == "SECRET" for h in a)
    assert run_hermie.scan_file(f, tmp_path, "data/r/src/b.md", tmp_path / "tmp") == []


def test_scan_file_detector_error_covers_file(tmp_path):
    make_file(tmp_path, "data/r/src/a.py", "one\ntwo\nthree\n")

    class Broken:
        def scan(self, text, origin, path_hint=None):
            return SimpleNamespace(findings=[], sensitive=True, reason="detector_error: X")

    got = run_hermie.scan_file(lambda d: Broken(), tmp_path, "data/r/src/a.py", tmp_path / "tmp")
    assert got == [hits.Hit("hermie", "data/r/src/a.py", 1, 3, None, None, "DETECTOR_ERROR")]


def test_scan_file_held_covers_file(tmp_path):
    make_file(tmp_path, "data/r/src/a.py", "one\ntwo")

    class Judged:
        def scan(self, text, origin, path_hint=None):
            return SimpleNamespace(findings=[], sensitive=True, reason="judge")

    got = run_hermie.scan_file(lambda d: Judged(), tmp_path, "data/r/src/a.py", tmp_path / "tmp")
    assert got == [hits.Hit("hermie", "data/r/src/a.py", 1, 2, None, None, "HELD")]


def test_scan_file_invalid_utf8(tmp_path, analyzer):
    make_file(tmp_path, "data/r/src/a.bin", b"\xff\xfe\x00junk\nmore\n")
    run_hermie.scan_file(gate_factory(analyzer), tmp_path, "data/r/src/a.bin", tmp_path / "tmp")


def test_resume_skips_done_and_drops_partial(tmp_path):
    a = hits.Hit("hermie", "data/r/a", 1, 1, 0, 3, "SECRET")
    b = hits.Hit("hermie", "data/r/b", 2, 2, 0, 3, "SECRET")
    hits.write_hits(tmp_path / "hermie.jsonl", [a, b])
    (tmp_path / "hermie_done.txt").write_text("data/r/a\n")
    assert run_hermie.resume_state(tmp_path) == {"data/r/a"}
    assert hits.read_hits(tmp_path / "hermie.jsonl") == [a]


def test_manifest_fields(tmp_path):
    cred = tmp_path / "cred"
    v = random_alnum(32)
    make_file(cred, "data/r1/src/a.py", 'api_secret = "' + v + '"\n')
    make_file(cred, "data/r1/src/b.txt", "nothing here\n")
    write_meta(cred / "meta", "r1", [
        "1,a,GitHub,r1,data/r1/src/a.py,1,1,T,14,46,,,Secret",
        "2,b,GitHub,r1,data/r1/src/b.txt,1,1,F,,,,,Password",
    ])
    out = tmp_path / "out"
    assert run_hermie.main(["--cred-root", str(cred), "--out", str(out), "--workers", "1"]) == 0
    m = json.loads((out / "hermie_manifest.json").read_text())
    for key in ("git_commit", "config", "files", "files_invalid_utf8", "detector_errors", "held", "seconds",
                "per_worker_peak_rss_mb", "slowest"):
        assert key in m
    assert m["files"] == 2
    assert any(h.file == "data/r1/src/a.py" and h.kind == "SECRET" for h in hits.read_hits(out / "hermie.jsonl"))
    assert (out / "hermie_done.txt").read_text().split() == ["data/r1/src/a.py", "data/r1/src/b.txt"]
