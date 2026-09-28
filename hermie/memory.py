"""Lesson memory: what went wrong before and what fixed it, recalled for similar tasks.

Lessons are one-line local-model outputs (after a review-then-fix episode) or reviewer problems that stayed unresolved,
plus lessons the user writes into AGENT.md by hand. They live in data_dir/lessons.jsonl with tags (workspace id, task
type, tools used) and two local embeddings: of the lesson text and of the task that produced it (the task text itself
is never stored). Recall: lessons of the same workspace always qualify; others need similarity >= LESSONS_MIN_SIM;
ranked by similarity weighted by how often they helped. Nothing here is ever sent to the cloud.
"""
from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import tempfile
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Protocol

import httpx

from .audit import sha256
from .config import Settings

log = logging.getLogger(__name__)


def workspace_id(path: Path) -> str:
    return sha256(str(Path(path).expanduser().resolve()))[:16]


@dataclass
class Lesson:
    id: str
    text: str
    workspace: str
    task_type: str
    tools: list[str]
    source: str                                   # review_fixed / repeated_failure / manual
    ts: str
    embedding: Optional[list[float]] = None       # of the lesson text
    key_embedding: Optional[list[float]] = None   # of the task that produced it (text not stored)
    uses: int = 0
    helped: int = 0
    disabled: bool = False


class EmbedderLike(Protocol):
    failed: bool

    def embed(self, text: str) -> Optional[list[float]]: ...


class Embedder:
    """Local embeddings through Ollama's /api/embed. Any failure returns None and sets `failed` (recall then falls back
    to word overlap); the caller decides whether to tell the user."""

    def __init__(self, settings: Settings, client: Optional[httpx.Client] = None):
        self.s = settings
        self.http = client or httpx.Client(timeout=30)
        self.failed = False

    def embed(self, text: str) -> Optional[list[float]]:
        if not text.strip():
            return None
        try:
            r = self.http.post(f"{self.s.ollama_url}/api/embed",
                               json={"model": self.s.lesson_embed_model, "input": [text]})
            r.raise_for_status()
            return [float(x) for x in r.json()["embeddings"][0]]
        except Exception as e:
            if not self.failed:
                log.warning("Lesson embeddings unavailable (%s); recall falls back to word overlap", type(e).__name__)
            self.failed = True
            return None


_WORD = re.compile(r"[a-z0-9_]{3,}")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _overlap(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


class LessonStore:
    def __init__(self, path: Path, embedder: EmbedderLike):
        self.path = Path(path)
        self.embedder = embedder
        self._lock = threading.Lock()
        self._lessons: list[Lesson] = self._read()

    def _read(self) -> list[Lesson]:
        out: list[Lesson] = []
        if not self.path.exists():
            return out
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(Lesson(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                log.warning("Skipping a malformed line in %s", self.path)
        return out

    def all(self) -> list[Lesson]:
        return list(self._lessons)

    def _save(self) -> None:
        """Merge with what is on disk (another Hermie instance may have written since we loaded), then replace the file
        atomically. An flock on a sidecar file serializes the read-merge-write across processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "w") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            mine = {l.id: l for l in self._lessons}
            for d in self._read():
                m = mine.get(d.id)
                if m is None:
                    self._lessons.append(d)
                    mine[d.id] = d
                else:
                    m.uses, m.helped = max(m.uses, d.uses), max(m.helped, d.helped)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(asdict(l), ensure_ascii=False) + "\n" for l in self._lessons))
            os.replace(tmp, self.path)

    def add(self, text: str, *, workspace: str, task_type: str, tools: list[str], source: str,
            key_text: str = "") -> Lesson:
        text = text.strip()
        with self._lock:
            for l in self._lessons:
                if l.workspace == workspace and l.text == text:
                    if l.disabled and source != "manual":
                        l.disabled = False
                        self._save()
                    return l
        lesson = Lesson(id=uuid.uuid4().hex[:12], text=text, workspace=workspace, task_type=task_type,
                        tools=sorted(set(tools)), source=source, ts=time.strftime("%Y-%m-%dT%H:%M:%S"),
                        embedding=self.embedder.embed(text),
                        key_embedding=self.embedder.embed(key_text) if key_text else None)
        with self._lock:
            self._lessons.append(lesson)
            self._save()
        return lesson

    def _similarity(self, l: Lesson, query: str, q_vec: Optional[list[float]]) -> float:
        if q_vec is not None and l.embedding is not None:
            return max(_cos(q_vec, l.embedding), _cos(q_vec, l.key_embedding or []))
        return _overlap(query, l.text)

    def recall(self, query: str, *, workspace: str, task_type: str, k: int, min_sim: float) -> list[Lesson]:
        live = [l for l in self._lessons if not l.disabled]
        if not live or k <= 0:
            return []
        q_vec = self.embedder.embed(query)
        scored = []
        for l in live:
            sim = self._similarity(l, query, q_vec)
            same_ws = l.workspace == workspace
            if not same_ws and sim < min_sim:
                continue
            score = sim * (1 + l.helped) / (1 + max(l.uses, l.helped))  # <= sim: history only ranks down
            scored.append((round(score, 4), same_ws, l.task_type == task_type, l.ts, l))
        scored.sort(key=lambda x: x[:4], reverse=True)
        return [x[-1] for x in scored[:k]]

    def feedback(self, ids: list[str], helped: bool) -> None:
        with self._lock:
            by_id = {l.id: l for l in self._lessons}
            for i in ids:
                if i in by_id:
                    by_id[i].uses += 1
                    by_id[i].helped += int(helped)
            self._save()

    def sync_doc(self, workspace: str, doc_lessons: Optional[list[str]], cap: int) -> int:
        """Reconcile with the AGENT.md "Lessons" section of this workspace: lessons written there by hand are imported
        (source "manual"); when the section is below its trimming cap, store lessons of this workspace that are missing
        from it were deleted by the user and are disabled. doc_lessons None (no AGENT.md, no Lessons section, or an
        unreadable file) changes nothing. Returns how many lessons were imported."""
        if doc_lessons is None:
            return 0
        doc = [x.strip() for x in doc_lessons if x.strip()]
        known = {l.text for l in self._lessons if l.workspace == workspace}
        imported = 0
        for text in doc:
            if text not in known:
                self.add(text, workspace=workspace, task_type="", tools=[], source="manual")
                imported += 1
        if len(doc) < cap:
            with self._lock:
                changed = False
                for l in self._lessons:
                    if l.workspace == workspace and not l.disabled and l.text not in doc:
                        l.disabled, changed = True, True
                if changed:
                    self._save()
        return imported
