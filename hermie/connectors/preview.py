"""What the model sees of a connector result: a row count, the field names and the first rows, capped. The full
output is in the data room, where the executor computes numbers with code."""
from __future__ import annotations

import json


def cap_text(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n... [truncated; {len(text) - cap} more chars in the saved file]"


def _flatten(row):
    if isinstance(row, dict) and isinstance(row.get("attributes"), dict):   # JSON:API (App Store Connect)
        return {"id": row.get("id"), **row["attributes"]}
    return row


def preview_json(text: str, cap: int) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return cap_text(text, cap)
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        rows = data["data"]
    elif isinstance(data, list):
        rows = data
    else:
        return cap_text(json.dumps(data, ensure_ascii=False, indent=1), cap)
    flat = [_flatten(r) for r in rows]
    keys = sorted({k for r in flat if isinstance(r, dict) for k in r})[:40]
    head = f"{len(rows)} rows; fields: {', '.join(keys)}\n"
    return cap_text(head + "\n".join(json.dumps(r, ensure_ascii=False) for r in flat), cap)


def preview_table(text: str, cap: int) -> str:
    lines = text.splitlines()
    head = f"{max(len(lines) - 1, 0)} rows; columns: {lines[0] if lines else ''}\n"
    return cap_text(head + "\n".join(lines[1:21]), cap)
