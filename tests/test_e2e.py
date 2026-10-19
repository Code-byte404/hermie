"""End-to-end canary suite (evals/e2e): generator, canary search, recorder, tasks, runner, scorer. No network."""
import json
import re
import subprocess
import sys
from itertools import permutations
from pathlib import Path

import pytest

from evals.e2e import workspace as W

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("ws") / "w"
    return out, W.build(1, out)


def _files(out: Path) -> dict[str, bytes]:
    return {str(p.relative_to(out)): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}


def test_build_is_deterministic(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    W.build(7, a); W.build(7, b); W.build(8, c)
    assert _files(a) == _files(b)
    assert (a / "canaries.json").read_text() != (c / "canaries.json").read_text()


def test_every_canary_appears_in_its_file(built):
    out, canaries = built
    assert canaries == W.load_canaries(out / "canaries.json")
    for c in canaries:
        assert c.value in (out / c.file).read_text(encoding="utf-8"), c.id


def test_no_canary_contains_another(built):
    _, canaries = built
    assert len({c.value for c in canaries}) == len(canaries) == len({c.id for c in canaries})
    norm = [(c.id, W.normalize(c.value)) for c in canaries]
    for (ia, a), (ib, b) in permutations(norm, 2):
        assert a not in b, (ia, ib)


def test_kinds_and_languages_are_covered(built):
    _, canaries = built
    assert {c.kind for c in canaries} == set(W.KINDS)
    for c in canaries:
        assert c.claimed == W.KINDS[c.kind]
    for kind in ("person", "phone", "address", "api_key", "company", "amount"):
        assert {c.lang for c in canaries if c.kind == kind} >= {"en", "zh"}, kind


def test_key_canaries_use_provider_shapes(built):
    _, canaries = built
    keys = [c for c in canaries if c.kind == "api_key"]
    for prefix in ("sk-ant-api03-", "AKIA", "ghp_", "xoxb-"):
        assert any(c.value.startswith(prefix) for c in keys), prefix
    files = {c.file for c in keys}
    assert "app/.env" in files and files & {"company/meeting_notes_zh.md", "mail/forwarded_en.eml"}


def test_refuses_repo_and_nonempty_paths(tmp_path):
    with pytest.raises(ValueError):
        W.build(1, Path(__file__).parent / "e2e-out")
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x")
    with pytest.raises(ValueError):
        W.build(1, tmp_path / "full")


def test_no_cjk_in_repo_sources():
    for p in (ROOT / "evals" / "e2e").rglob("*"):
        if p.is_file() and p.suffix in (".py", ".json", ".md", ".txt"):
            assert not re.search(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]", p.read_text(encoding="utf-8")), p


def test_app_bug_is_real(built):
    out, _ = built
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=out / "app",
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1, r.stdout[-500:]


def test_top_three_salaries_are_fixed_positions(built):
    out, canaries = built
    by_id = {c.id: c for c in canaries}
    rows = (out / "company" / "employees.csv").read_text(encoding="utf-8").splitlines()[1:]
    salaries = sorted((int(r.split(",")[4]), i) for i, r in enumerate(rows))
    assert [i for _, i in salaries[-3:]][::-1] == list(W.TOP_PAID)
    assert all(f"emp.{i}.name" in by_id for i in W.TOP_PAID)
