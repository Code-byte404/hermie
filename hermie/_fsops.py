"""File operations executed inside the sandbox subprocess. The main process never touches the workspace
directly; the file boundary is enforced by the kernel alone.

Usage: python -I _fsops.py <op>; arguments come as JSON on stdin, the result is written as JSON to stdout.
Standard library only, because it runs in -I (isolated) mode.
"""
import json
import os
import sys


def _resolve(root, path):
    """Resolve a relative path inside the workspace; refuse it if, after resolution (including symlinks), it is
    not inside the workspace. Second line of defense behind the sandbox, and the only boundary in no-sandbox mode."""
    root_real = os.path.realpath(root)
    p = os.path.realpath(os.path.join(root, path))
    if os.path.commonpath([root_real, p]) != root_real:
        raise PermissionError(f"path is outside the workspace: {path}")
    return p


def op_read(a):
    p = _resolve(a["root"], a["path"])
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        data = f.read()
    start = max(0, int(a.get("offset", 0)))
    limit = int(a.get("max_chars", 8000))
    chunk = data[start:start + limit]
    return {"content": chunk, "total_chars": len(data), "truncated": start + limit < len(data)}


def op_write(a):
    p = _resolve(a["root"], a["path"])
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    existed = os.path.exists(p)
    with open(p, "w", encoding="utf-8") as f:
        f.write(a["content"])
    return {"path": a["path"], "bytes": len(a["content"].encode("utf-8")), "overwrote": existed}


def op_edit(a):
    p = _resolve(a["root"], a["path"])
    with open(p, "r", encoding="utf-8") as f:
        data = f.read()
    n = data.count(a["old"])
    if n != 1:
        return {"error": f"the old text occurs {n} times; it must occur exactly once"}
    with open(p, "w", encoding="utf-8") as f:
        f.write(data.replace(a["old"], a["new"], 1))
    return {"path": a["path"], "replaced": 1}


def op_list(a):
    base = _resolve(a["root"], a.get("path", "."))
    depth = int(a.get("depth", 2))
    out = []
    base_depth = base.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".git"))
        level = dirpath.count(os.sep) - base_depth
        if level >= depth:
            dirnames[:] = []
        rel = os.path.relpath(dirpath, a["root"])
        for fn in sorted(filenames):
            fp = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(fp)
            except OSError:
                size = -1
            out.append({"path": os.path.normpath(os.path.join(rel, fn)), "size": size})
        if len(out) > 500:
            break
    return {"files": out[:500], "truncated": len(out) > 500}


OPS = {"read": op_read, "write": op_write, "edit": op_edit, "list": op_list}

if __name__ == "__main__":
    args = json.loads(sys.stdin.read())
    try:
        res = OPS[sys.argv[1]](args)
    except Exception as e:  # including the PermissionError raised when the sandbox refuses
        res = {"error": f"{type(e).__name__}: {e}"}
    sys.stdout.write(json.dumps(res, ensure_ascii=False))
