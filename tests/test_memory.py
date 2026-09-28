"""Lesson memory: a local store with local embeddings; recall prefers the same workspace, then similarity,
then lessons that helped."""
import json
import math
from pathlib import Path

import httpx

from hermie.config import Settings
from hermie.memory import Embedder, LessonStore, workspace_id

from .conftest import FakeEmbedder


class DeadEmbedder:
    failed = True

    def embed(self, text):
        return None


WS_A, WS_B = workspace_id(Path("/tmp/a")), workspace_id(Path("/tmp/b"))


def test_add_persists_and_dedupes(tmp_path):
    store = LessonStore(tmp_path / "lessons.jsonl", FakeEmbedder())
    a = store.add("Use csv instead of openpyxl for spreadsheet files.", workspace=WS_A, task_type="repetitive",
                  tools=["write_file"], source="review_fixed", key_text="Process the spreadsheet")
    again = store.add("Use csv instead of openpyxl for spreadsheet files.", workspace=WS_A, task_type="repetitive",
                      tools=[], source="review_fixed")
    assert again.id == a.id and len(store.all()) == 1
    reloaded = LessonStore(tmp_path / "lessons.jsonl", FakeEmbedder())
    [l] = reloaded.all()
    assert l.text.startswith("Use csv") and l.embedding and l.key_embedding and l.tools == ["write_file"]
    assert "Process the spreadsheet" not in (tmp_path / "lessons.jsonl").read_text()  # only its vector is stored


def test_recall_same_workspace_always_qualifies_other_needs_similarity(tmp_path):
    store = LessonStore(tmp_path / "l.jsonl", FakeEmbedder())
    store.add("Run swift build before claiming the xcode project compiles.", workspace=WS_A, task_type="complex",
              tools=[], source="review_fixed")
    store.add("Use csv instead of openpyxl for spreadsheet files.", workspace=WS_B, task_type="repetitive",
              tools=[], source="review_fixed")
    store.add("Docker is not available in the sandbox.", workspace=WS_B, task_type="simple", tools=[],
              source="review_fixed")
    got = store.recall("Convert this excel spreadsheet to csv", workspace=WS_A, task_type="repetitive", k=5, min_sim=0.3)
    texts = [l.text for l in got]
    assert texts[0].startswith("Use csv")                  # similar, other workspace
    assert any(t.startswith("Run swift") for t in texts)   # dissimilar, but same workspace
    assert not any(t.startswith("Docker") for t in texts)  # dissimilar, other workspace


def test_recall_respects_k_disabled_and_ranks_helpful(tmp_path):
    store = LessonStore(tmp_path / "l.jsonl", FakeEmbedder())
    a = store.add("Use csv for spreadsheet files.", workspace=WS_A, task_type="x", tools=[], source="review_fixed")
    b = store.add("Prefer csv for excel spreadsheet exports.", workspace=WS_A, task_type="x", tools=[], source="review_fixed")
    c = store.add("Check the csv header row of spreadsheet files.", workspace=WS_A, task_type="x", tools=[], source="review_fixed")
    store.feedback([b.id], helped=True)
    store.feedback([a.id, a.id, a.id], helped=False)
    got = store.recall("spreadsheet csv", workspace=WS_A, task_type="x", k=2, min_sim=0.1)
    assert [l.id for l in got][0] == b.id and len(got) == 2
    store.sync_doc(WS_A, ["Prefer csv for excel spreadsheet exports.", "Check the csv header row of spreadsheet files."], cap=20)
    assert a.id not in [l.id for l in store.recall("spreadsheet csv", workspace=WS_A, task_type="x", k=5, min_sim=0.1)]


def test_feedback_counts_persist(tmp_path):
    store = LessonStore(tmp_path / "l.jsonl", FakeEmbedder())
    a = store.add("Use csv.", workspace=WS_A, task_type="x", tools=[], source="review_fixed")
    store.feedback([a.id], helped=True)
    store.feedback([a.id], helped=False)
    [l] = LessonStore(tmp_path / "l.jsonl", FakeEmbedder()).all()
    assert l.uses == 2 and l.helped == 1


def test_sync_doc_imports_manual_and_respects_cap(tmp_path):
    store = LessonStore(tmp_path / "l.jsonl", FakeEmbedder())
    store.add("Old lesson about docker.", workspace=WS_A, task_type="x", tools=[], source="review_fixed")
    assert store.sync_doc(WS_A, ["Hand written: run pytest -q before done."], cap=20) == 1
    by_text = {l.text: l for l in store.all()}
    assert by_text["Hand written: run pytest -q before done."].source == "manual"
    assert by_text["Old lesson about docker."].disabled  # doc not full: missing from it means the user deleted it
    store2 = LessonStore(tmp_path / "l2.jsonl", FakeEmbedder())
    store2.add("Kept even though trimmed.", workspace=WS_A, task_type="x", tools=[], source="review_fixed")
    store2.sync_doc(WS_A, [f"lesson {i}" for i in range(20)], cap=20)
    assert not next(l for l in store2.all() if l.text == "Kept even though trimmed.").disabled


def test_store_degrades_without_embeddings(tmp_path):
    store = LessonStore(tmp_path / "l.jsonl", DeadEmbedder())
    store.add("Use csv instead of openpyxl for spreadsheet files.", workspace=WS_B, task_type="x", tools=[],
              source="review_fixed")
    store.add("Docker is not available.", workspace=WS_B, task_type="x", tools=[], source="review_fixed")
    got = store.recall("convert the spreadsheet files to csv", workspace=WS_A, task_type="x", k=5, min_sim=0.1)
    assert [l.text for l in got] == ["Use csv instead of openpyxl for spreadsheet files."]


def test_embedder_calls_ollama_and_fails_soft():
    seen = []

    def ok(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})
    e = Embedder(Settings(ollama_url="http://o.test"), client=httpx.Client(transport=httpx.MockTransport(ok)))
    assert e.embed("hello") == [0.1, 0.2] and seen[0] == {"model": "nomic-embed-text", "input": ["hello"]}
    bad = Embedder(Settings(ollama_url="http://o.test"),
                   client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))))
    assert bad.embed("hello") is None and bad.failed
