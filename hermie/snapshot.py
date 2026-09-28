"""Pre-task snapshots and one-step rollback. The sandbox stops out-of-bounds writes; it cannot stop files
inside the boundary from being broken.

- git repositories: the whole working tree (including untracked, excluding ignored files) is written as one
  commit stored under refs/hermie/snapshots/<id>; the user's branches, index and history are untouched.
- other directories: APFS copy-on-write clone (cp -c, near-instant and takes no extra space); falls back to a
  plain copy when unsupported.

Snapshots are taken by the main process (a trusted operation of the framework itself, not one requested by
the model).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

REF_PREFIX = "refs/hermie/snapshots/"


@dataclass
class Snapshot:
    id: str
    kind: str   # git / clone
    ts: float
    workspace: str
    label: str
    ref: str    # git: commit hash; clone: clone directory


def _git(ws: Path, *args: str, env: Optional[dict] = None, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _is_git_root(ws: Path) -> bool:
    try:
        return Path(_git(ws, "rev-parse", "--show-toplevel")).resolve() == ws.resolve()
    except (RuntimeError, FileNotFoundError):
        return False


class SnapshotManager:
    def __init__(self, workspace: Path, store: Path):
        self.ws = workspace.resolve()
        self.store = store.resolve()
        self.store.mkdir(parents=True, exist_ok=True)
        self.index = self.store / "index.jsonl"

    # ------------------------------------------------------------ create
    def take(self, label: str = "", keep: int = 0) -> Snapshot:
        sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        if _is_git_root(self.ws):
            snap = Snapshot(sid, "git", time.time(), str(self.ws), label, self._git_snapshot(sid))
        else:
            snap = Snapshot(sid, "clone", time.time(), str(self.ws), label, self._clone_snapshot(sid))
        with open(self.index, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(snap), ensure_ascii=False) + "\n")
        if keep > 0:
            self.prune(keep)
        return snap

    def prune(self, keep: int) -> list[Snapshot]:
        """Keep only the most recent `keep` snapshots of this workspace; delete older clone directories / git
        refs and rewrite the index. Returns the dropped snapshots."""
        mine = self.list()
        drop = mine[:-keep] if keep > 0 else mine
        if not drop:
            return []
        for snap in drop:
            try:
                if snap.kind == "git":
                    _git(self.ws, "update-ref", "-d", REF_PREFIX + snap.id, check=False)
                else:
                    shutil.rmtree(snap.ref, ignore_errors=True)
            except Exception:
                pass
        dropped = {s.id for s in drop}
        lines = [l for l in self.index.read_text(encoding="utf-8").splitlines()
                 if l.strip() and json.loads(l)["id"] not in dropped]
        self.index.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
        return drop

    def _tree_of_worktree(self, index_file: str) -> str:
        env = {"GIT_INDEX_FILE": index_file}
        _git(self.ws, "add", "-A", ".", env=env)
        return _git(self.ws, "write-tree", env=env)

    def _git_snapshot(self, sid: str) -> str:
        with tempfile.TemporaryDirectory() as td:
            idx = os.path.join(td, "index")
            head = _git(self.ws, "rev-parse", "--verify", "-q", "HEAD", check=False)
            if head:  # start from HEAD's index to speed up `add` in large repositories
                _git(self.ws, "read-tree", head, env={"GIT_INDEX_FILE": idx})
            tree = self._tree_of_worktree(idx)
        args = ["commit-tree", tree, "-m", f"hermie snapshot {sid}"]
        if head:
            args[2:2] = ["-p", head]
        commit = _git(self.ws, *args, env={"GIT_AUTHOR_NAME": "hermie", "GIT_AUTHOR_EMAIL": "local@hermie",
                                            "GIT_COMMITTER_NAME": "hermie",
                                            "GIT_COMMITTER_EMAIL": "local@hermie"})
        _git(self.ws, "update-ref", REF_PREFIX + sid, commit)
        return commit

    def _clone_snapshot(self, sid: str) -> str:
        dest = self.store / sid
        r = subprocess.run(["cp", "-cR", str(self.ws), str(dest)], capture_output=True, text=True)
        if r.returncode != 0:  # not APFS or across volumes: fall back to a plain copy
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(self.ws, dest, symlinks=True)
        return str(dest)

    # ------------------------------------------------------------ query and rollback
    def list(self) -> list[Snapshot]:
        if not self.index.exists():
            return []
        out = []
        for line in self.index.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            if d["workspace"] == str(self.ws):
                out.append(Snapshot(**d))
        return out

    def latest(self) -> Optional[Snapshot]:
        snaps = self.list()
        return snaps[-1] if snaps else None

    def restore(self, snap: Snapshot) -> None:
        if snap.kind == "git":
            self._git_restore(snap.ref)
        else:
            self._clone_restore(Path(snap.ref))

    def diff(self, snap: Snapshot, max_chars: int = 200_000) -> str:
        """Difference between the snapshot and the current workspace (unified diff), shown in the UI's "Changes" tab."""
        if snap.kind == "git":
            with tempfile.TemporaryDirectory() as td:
                tree = self._tree_of_worktree(os.path.join(td, "now"))
                out = _git(self.ws, "diff", "--no-color", "--no-ext-diff", snap.ref, tree, check=False)
        else:
            r = subprocess.run(["diff", "-ruN", "--exclude=.git", snap.ref, str(self.ws)],
                               capture_output=True, text=True, errors="replace")
            out = r.stdout.replace(snap.ref, "snapshot").replace(str(self.ws), "workspace")
        return out[:max_chars]

    def _git_restore(self, commit: str) -> None:
        with tempfile.TemporaryDirectory() as td:
            now_idx, snap_idx = os.path.join(td, "now"), os.path.join(td, "snap")
            self._tree_of_worktree(now_idx)
            now_files = set(_git(self.ws, "ls-files", env={"GIT_INDEX_FILE": now_idx}).splitlines())
            _git(self.ws, "read-tree", commit, env={"GIT_INDEX_FILE": snap_idx})
            snap_files = set(_git(self.ws, "ls-files", env={"GIT_INDEX_FILE": snap_idx}).splitlines())
            for rel in now_files - snap_files:  # files created after the snapshot
                (self.ws / rel).unlink(missing_ok=True)
            _git(self.ws, "checkout-index", "-a", "-f", env={"GIT_INDEX_FILE": snap_idx})
        # Note: the user's real index (staging area) has not been touched

    def _clone_restore(self, src: Path) -> None:
        if not src.exists():
            raise FileNotFoundError(f"Snapshot directory does not exist: {src}")
        for child in self.ws.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in src.iterdir():
            dest = self.ws / child.name
            r = subprocess.run(["cp", "-cR", str(child), str(dest)], capture_output=True)
            if r.returncode != 0:
                if child.is_dir() and not child.is_symlink():
                    shutil.copytree(child, dest, symlinks=True)
                else:
                    shutil.copy2(child, dest, follow_symlinks=False)
