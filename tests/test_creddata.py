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
