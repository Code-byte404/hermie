"""Skill library: procedures that worked before, as Markdown playbooks the user can read and edit.

A skill is a file in data_dir/skills/<slug>.md with a small front matter (id, title, status) and three sections:
"## When to use", "## Steps", "## Verify". The local compressor model distills it from a step that passed review
(core._skills_after_task); it starts as a candidate and becomes active after a second similar success (merge
similarity >= SKILL_MERGE_SIM) or /skills approve. Only active skills are recalled (similarity >= SKILLS_MIN_SIM, for
every skill, same project included) and injected into the executor prompt; a skill used SKILL_RETIRE_USES times with
a first-review pass rate under SKILL_RETIRE_RATE retires itself.

index.jsonl holds what the user should not have to edit: status mirror, counters, embeddings (of the playbook and of
the step that produced it; that step's text is not stored) and the file mtime Hermie last saw, so that a user's edit
is noticed and Hermie's own writes are not. Nothing here is ever sent to the cloud.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .config import Settings
from .memory import EmbedderLike, _cos, _overlap

log = logging.getLogger(__name__)

HEADINGS = ("## When to use", "## Steps", "## Verify")
STATUSES = ("candidate", "active", "retired")
SKILL_RELATED_SIM = 0.7   # playbook similarity required when the steps match (see add_candidate)
_FRONT = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


@dataclass
class Skill:
    id: str
    slug: str
    title: str
    status: str                                   # candidate / active / retired
    source: str                                   # distilled / manual
    workspace: str
    task_type: str
    created: str
    confirmations: int = 0
    uses: int = 0
    helped: int = 0
    embedding: Optional[list[float]] = None
    key_embedding: Optional[list[float]] = None
    mtime: float = 0.0


def parse_skill(text: str) -> Optional[tuple[dict, str]]:
    """(front matter, body) of a skill file, or None when it is not one (no front matter, no title, or a section
    missing)."""
    m = _FRONT.match(text.replace("\r\n", "\n"))
    if not m:
        return None
    front = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            front[k.strip()] = v.strip()
    body = m.group(2).strip()
    if not front.get("title") or not all(h in body for h in HEADINGS):
        return None
    return front, body


def _render(skill: Skill, body: str) -> str:
    return f"---\nid: {skill.id}\ntitle: {skill.title}\nstatus: {skill.status}\n---\n\n{body.strip()}\n"


def _slug(title: str, sid: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "skill"
    return f"{base}-{sid[:6]}"


class SkillStore:
    def __init__(self, directory: Path, embedder: EmbedderLike, settings: Settings):
        self.dir = Path(directory)
        self.index_path = self.dir / "index.jsonl"
        self.embedder = embedder
        self.s = settings
        self._lock = threading.Lock()
        self.dir.mkdir(parents=True, exist_ok=True)
        self._skills: dict[str, Skill] = {k.id: k for k in self._read_index()}
        self.sync()

    # ------------------------------------------------------------ persistence
    def _read_index(self) -> list[Skill]:
        out = []
        if not self.index_path.exists():
            return out
        for line in self.index_path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(Skill(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                log.warning("Skipping a malformed line in %s", self.index_path)
        return out

    def _save(self) -> None:
        """Merge with the index on disk under an flock (another Hermie may have written), then replace it atomically."""
        with open(self.dir / "index.lock", "w") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            for d in self._read_index():
                m = self._skills.get(d.id)
                if m is None:
                    if (self.dir / f"{d.slug}.md").exists():
                        self._skills[d.id] = d
                else:
                    m.uses, m.helped = max(m.uses, d.uses), max(m.helped, d.helped)
                    m.confirmations = max(m.confirmations, d.confirmations)
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix="index", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(asdict(k), ensure_ascii=False) + "\n" for k in self._skills.values()))
            os.replace(tmp, self.index_path)

    def path(self, skill: Skill) -> Path:
        return self.dir / f"{skill.slug}.md"

    def _write_file(self, skill: Skill, body: str, create: bool = False) -> None:
        p = self.path(skill)
        if not create and not p.exists():
            raise LookupError(f"skill file {p.name} no longer exists")
        p.write_text(_render(skill, body), encoding="utf-8")
        skill.mtime = p.stat().st_mtime

    def body(self, skill: Skill) -> str:
        parsed = parse_skill(self.path(skill).read_text(encoding="utf-8")) if self.path(skill).exists() else None
        return parsed[1] if parsed else ""

    def _embed(self, title: str, body: str) -> Optional[list[float]]:
        return self.embedder.embed(f"{title}\n{body}")

    # ------------------------------------------------------------ sync with the files the user may edit
    def sync(self) -> bool:
        """Apply user edits: new files are imported (active, manual), edited files re-read and re-embedded, deleted
        files dropped. Malformed files are skipped and left alone. Returns whether anything changed."""
        changed = False
        seen: set[str] = set()
        by_slug = {k.slug: k for k in self._skills.values()}
        for p in sorted(self.dir.glob("*.md")):
            try:
                text = p.read_text(encoding="utf-8")
                mtime = p.stat().st_mtime
            except OSError:
                continue
            parsed = parse_skill(text)
            if parsed is None:
                continue
            front, body = parsed
            sk = self._skills.get(front.get("id", "")) or by_slug.get(p.stem)
            if sk is None:  # a file the user dropped in
                sk = Skill(id=front.get("id") or uuid.uuid4().hex[:12], slug=p.stem, title=front["title"],
                           status=front.get("status") if front.get("status") in STATUSES else "active",
                           source="manual", workspace="", task_type="", created=time.strftime("%Y-%m-%dT%H:%M:%S"),
                           embedding=self._embed(front["title"], body))
                self._skills[sk.id] = sk
                sk.mtime = mtime
                if "id" not in front:
                    try:
                        self._write_file(sk, body)  # writes the id back so the file keeps its identity
                    except OSError:  # read-only file: keep it as it is, match it by name next time
                        log.warning("Skill file %s is read-only; imported without writing its id back", p)
                changed = True
            elif mtime != sk.mtime:
                sk.slug, sk.title = p.stem, front["title"]
                if front.get("status") in STATUSES:
                    sk.status = front["status"]
                sk.embedding = self._embed(sk.title, body)
                sk.mtime = mtime
                changed = True
            if sk.slug != p.stem:  # renamed by the user (mv keeps the mtime)
                sk.slug = p.stem
                changed = True
            seen.add(sk.id)
        for sid in [k for k in self._skills if k not in seen]:
            del self._skills[sid]
            changed = True
        if changed:
            self._save()
        return changed

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    # ------------------------------------------------------------ writers
    def _similarity(self, sk: Skill, text: str, vec: Optional[list[float]]) -> float:
        if vec is not None and sk.embedding is not None:
            return max(_cos(vec, sk.embedding), _cos(vec, sk.key_embedding or []))
        return _overlap(text, f"{sk.title}\n{self.body(sk)}")

    def add_candidate(self, title: str, body: str, *, workspace: str, task_type: str,
                      key_text: str = "") -> tuple[Skill, bool]:
        """A freshly distilled playbook: confirms (and activates) a similar existing skill, or becomes a candidate."""
        title, body = title.strip(), body.strip()
        vec = self._embed(title, body)
        key_vec = self.embedder.embed(key_text) if key_text else None
        with self._lock:
            best = None
            if vec is not None:  # without embeddings nothing is merged: two candidates are safer than a wrong merge
                best_score = 0.0
                for sk in self._skills.values():
                    if sk.status == "retired" or sk.embedding is None:
                        continue
                    pb = _cos(vec, sk.embedding)                                    # playbook vs playbook
                    ks = _cos(key_vec, sk.key_embedding) if key_vec and sk.key_embedding else 0.0  # step vs step
                    # the same kind of step, written up differently: accept a looser playbook match
                    ok = pb >= self.s.skill_merge_sim or (ks >= self.s.skill_merge_sim and pb >= SKILL_RELATED_SIM)
                    if ok and max(pb, ks) > best_score:
                        best, best_score = sk, max(pb, ks)
            if best is not None:
                best.confirmations += 1
                if best.status == "candidate":
                    best.status = "active"
                    self._write_file(best, self.body(best))
                self._save()
                return best, True
            sid = uuid.uuid4().hex[:12]
            sk = Skill(id=sid, slug=_slug(title, sid), title=title, status="candidate", source="distilled",
                       workspace=workspace, task_type=task_type, created=time.strftime("%Y-%m-%dT%H:%M:%S"),
                       embedding=vec, key_embedding=key_vec)
            self._skills[sid] = sk
            self._write_file(sk, body, create=True)
            self._save()
            return sk, False

    def recall(self, query: str, *, k: int, min_sim: float) -> list[Skill]:
        self.sync()
        live = [s for s in self._skills.values() if s.status == "active"]
        if not live or k <= 0:
            return []
        vec = self.embedder.embed(query)
        if vec is None:  # word overlap is too weak to pick procedures (measured); skills pause until embeddings return
            return []
        scored = []
        for sk in live:
            sim = self._similarity(sk, query, vec)
            if sim < min_sim:
                continue
            scored.append((round(sim * (1 + sk.helped) / (1 + max(sk.uses, sk.helped)), 4), sk.created, sk))
        scored.sort(key=lambda x: x[:2], reverse=True)
        return [x[-1] for x in scored[:k]]

    def feedback(self, ids: list[str], helped: bool) -> list[Skill]:
        """Count a use (and a help); retire skills that keep not helping. Returns the skills retired now."""
        retired = []
        with self._lock:
            for sid in ids:
                sk = self._skills.get(sid)
                if sk is None:
                    continue
                sk.uses += 1
                sk.helped += int(helped)
                if (sk.status == "active" and sk.uses >= self.s.skill_retire_uses
                        and sk.helped / sk.uses < self.s.skill_retire_rate):
                    sk.status = "retired"
                    self._write_file(sk, self.body(sk))
                    retired.append(sk)
            self._save()
        return retired

    def set_status(self, id_prefix: str, status: str) -> Skill:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        self.sync()  # pick up renames and edits before writing
        matches = [k for k in self._skills.values() if id_prefix and k.id.startswith(id_prefix)]
        if len(matches) != 1:
            raise LookupError(f"{'no' if not matches else 'more than one'} skill matches {id_prefix!r}")
        sk = matches[0]
        with self._lock:
            sk.status = status
            self._write_file(sk, self.body(sk))
            self._save()
        return sk
