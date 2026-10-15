"""Plain words for what Hermie did: the one place for every sentence a person reads (the Claude Code menu, hooks,
status line, terminal prompt, dialog) and for the notes and errors that leave the machine. Pure functions, no I/O.

Notes and error texts go upstream or to the client: they never take a value or a reason's free text, only ids,
sizes, tool names and fixed phrases. Display helpers take real values from the local mapping."""
from __future__ import annotations

from typing import Iterable

from hermie.gate.redact import PLACEHOLDER

SECRET_ENTITIES = frozenset({"SECRET"})
LONG_VALUE = 20      # a value this long without whitespace is shown shortened, like a key
SHOW_MAX = 80        # longest value shown in full
LIST_LIMIT = 5

SHORT_REASONS = frozenset({"judged private", "encoded data", "image", "could not be scanned",
                           "you chose not to send it", "held for review"})


def short_reason(reason: str) -> str:
    if reason == "judge":
        return "judged private"
    if reason.startswith("smuggling"):
        return "encoded data"
    if reason in ("image", "image released"):
        return "image"
    if reason.startswith("detector_error"):
        return "could not be scanned"
    if reason == "rejected earlier":
        return "you chose not to send it"
    return "held for review"


_REASON_TEXT = {
    "judged private": "a local model judged it private (personal or business facts)",
    "encoded data": "it contains a long encoded string that could carry data out",
    "image": "it is an image, and Hermie cannot read images",
    "could not be scanned": "Hermie could not scan it",
    "you chose not to send it": "you chose not to send it",
    "held for review": "Hermie held it for review",
}


def reason_text(reason: str) -> str:
    return _REASON_TEXT[short_reason(reason)]


def _entity(placeholder: str) -> str:
    m = PLACEHOLDER.fullmatch(placeholder)
    return m.group(1) if m else ""


def display_value(placeholder: str, value: str) -> str:
    flat = " ".join(str(value).split())
    if _entity(placeholder) in SECRET_ENTITIES or (len(flat) >= LONG_VALUE and " " not in flat):
        return f"{flat[:6]}…{flat[-3:]}" if len(flat) > 9 else "…"
    return flat if len(flat) <= SHOW_MAX else flat[:SHOW_MAX] + "…"


def value_pairs(placeholders: Iterable[str], mapping: dict, limit: int = LIST_LIMIT) -> list[tuple[str, str]]:
    seen: list[str] = []
    for ph in placeholders:
        if ph not in seen:
            seen.append(ph)
    out = [(display_value(ph, mapping[ph]) if isinstance(mapping.get(ph), str) else "", ph) for ph in seen[:limit]]
    if len(seen) > limit:
        out.append((f"+{len(seen) - limit} more", ""))
    return out


def pairs_text(pairs) -> str:
    return ", ".join(d if not ph else (f"{d} -> {ph}" if d else ph) for d, ph in pairs)


def names_text(pairs) -> str:
    return ", ".join(d or ph for d, ph in pairs)


# --- what leaves the machine: no values, no reason free text ---

def note_text(kind: str, id: str, size: str = "", reason: str = "", tool: str | None = None) -> str:
    what = ", ".join(x for x in (tool, size) if x)
    what = f" ({what})" if what else ""
    if kind == "unscanned":
        return (f"[Hermie withheld this tool result{what} because it could not be scanned, so it cannot be released. "
                f"Tell the user (id {id}).]")
    if kind == "image":
        return (f"[Hermie withheld an image because Hermie cannot read images. Tell the user, and that they can "
                f"release it with: hermie allow {id}]")
    if kind == "dropped":
        return (f"[Hermie withheld a message the user chose not to send (id {id}). If the user asks, it can be "
                f"released with: hermie allow {id}]")
    return (f"[Hermie withheld this tool result{what} because {reason_text(reason)}. Tell the user, and that they "
            f"can release it with: hermie allow {id}]")


def error_text(cause: str, id: str, timeout_s: float = 0) -> str:
    why = {
        "reject": "you chose Don't send",
        "timeout": f"no answer within {int(timeout_s)} seconds",
        "unavailable": "no one was asked (no menu, dialog or terminal was available)",
        "local_discarded": "you did not send the local rewrite",
        "local_failed": "the local model could not rewrite it, or the rewrite was too long to review",
    }.get(cause, "you chose Don't send")
    return (f"Hermie did not send this message: {why}. Nothing left this machine. Edit the prompt, or send it "
            f"anyway with: hermie allow {id}, then resend.")


UNSCANNED_ERROR = ("Hermie did not send this message: it could not be scanned, so it cannot be sent or released. "
                   "Shorten it or try again.")


def unreachable_text(host: str, error: str) -> str:
    return f"Hermie could not reach {host} ({error}). Nothing was sent. Check the network and try again."


def mapping_error_text(path, error: str) -> str:
    return (f"Hermie could not write its placeholder file {path} ({error}), so it sent nothing. Check the disk and "
            f"the file's permissions.")


# --- held prompt (menu, terminal, dialog) ---

OPTIONS: dict[str, tuple[str, str]] = {
    "reject": ("Don't send", "Nothing leaves. Claude Code shows an error; edit the prompt and try again."),
    "send": ("Send this one", "Sent as shown above. This exact text is not asked about again."),
    "session": ("Send all, this session", "Messages stopped by the judge or the encoded-data check go out without "
                                          "asking until hermie serve restarts. Replacing still applies."),
    "local": ("Rewrite locally", "Your local model hides the specifics; you review before anything is sent."),
}
REVIEW_OPTIONS: dict[str, tuple[str, str]] = {
    "reject": ("Don't send", "Nothing leaves. Claude Code shows an error."),
    "again": ("Redo", "The local model looks again."),
    "send": ("Send this", "Exactly the text above is sent."),
}


def held_view(row: dict, mapping: dict) -> dict:
    review = row.get("kind") == "rewrite"
    text = str(row.get("preview" if review else "excerpt") or "")
    phs = row.get("placeholders") or [m.group(0) for m in PLACEHOLDER.finditer(text)]
    if review:
        title, table, order = ("Your message with the specifics hidden: this is what would be sent", REVIEW_OPTIONS,
                               ["reject", "again", "send"])
    else:
        title, table = "Hermie stopped your message before it left this machine", OPTIONS
        order = ["reject", "send", "session"] + (["local"] if row.get("local") else [])
    return {
        "id": row.get("id"),
        "title": title,
        "why": reason_text(str(row.get("reason", ""))),
        "excerpt": text,
        "pairs": [[d, ph] for d, ph in value_pairs(phs, mapping)],
        "options": [{"value": v, "key": str(i + 1), "label": table[v][0], "note": table[v][1]}
                    for i, v in enumerate(order)],
        "deadline": row.get("deadline"),
    }


def held_lines(view: dict, keys: dict[str, str]) -> list[str]:
    lines = [view["title"], f"Why: {view['why']}"]
    if view["excerpt"]:
        lines.append(f'"{view["excerpt"]}"')
    if view["pairs"]:
        lines.append(f"Already replaced: {pairs_text(view['pairs'])}")
    for o in view["options"]:
        if o["value"] in keys:
            lines.append(f"  [{keys[o['value']]}] {o['label']}: {o['note']}")
    lines.append(f"No answer in time means Don't send. Later: hermie allow {view['id']}")
    return lines


# --- after the fact (Stop hook, status line, menu band) ---

def _placeholders(line) -> list[str]:
    return list(getattr(line, "placeholders", None) or [])


def _replaced_part(lines, mapping) -> str | None:
    phs = [ph for ln in lines for ph in _placeholders(ln)]
    if phs:
        return pairs_text(value_pairs(phs, mapping))
    counts: dict[str, int] = {}
    for ln in lines:   # lines written before `placeholders` existed: entity counts only
        for k, v in (getattr(ln, "replaced", None) or {}).items():
            counts[k] = max(counts.get(k, 0), int(v))
    return ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))) or None


def _withheld_part(lines) -> str | None:
    items: dict[str, str] = {}
    for ln in lines:
        known = {it.get("id"): it for it in (getattr(ln, "withheld_items", None) or []) if isinstance(it, dict)}
        for wid in getattr(ln, "withheld", None) or []:
            it = known.get(wid) or {}
            label = " ".join(x for x in (it.get("tool"), it.get("file")) if x) or "an item"
            why = it.get("why")
            items.setdefault(wid, f"{label} ({why})" if why else label)
    if not items:
        return None
    shown = list(items.items())[:LIST_LIMIT]
    more = f", +{len(items) - LIST_LIMIT} more" if len(items) > LIST_LIMIT else ""
    return ("; ".join(f"{label} -> hermie allow {wid}" for wid, label in shown) + more
            + " · the AI saw a note instead")


def _closing(lines) -> str:
    if any(getattr(ln, "mode", "") == "observe" for ln in lines):
        return "Observe mode: nothing was replaced, everything was sent."
    if any(getattr(ln, "approved_by", None) in ("user", "session", "allowed") for ln in lines):
        return "Not everything was replaced: items you approved went out as is."
    return "Only placeholders left this machine."


def turn_summary(lines, mapping: dict) -> str | None:
    lines = list(lines)
    rows = []
    if (rep := _replaced_part(lines, mapping)):
        rows.append(f"  replaced    {rep}")
    if (wh := _withheld_part(lines)):
        rows.append(f"  withheld    {wh}")
    held = [ln.held for ln in lines if getattr(ln, "held", None) and getattr(ln, "approved_by", None) is None]
    if held:
        rows.append(f"  held        {len(held)} message{'s' if len(held) != 1 else ''} not sent"
                    f" -> send anyway: hermie allow {held[-1]}")
    restored = sum(getattr(ln, "restored", 0) or 0 for ln in lines)
    if restored:
        rows.append(f"  put back    {restored} real value{'s' if restored != 1 else ''} in the replies")
    if not rows:
        return None
    n = len(lines)
    return "\n".join([f"Hermie · this turn ({n} request{'s' if n != 1 else ''})", *rows, f"  {_closing(lines)}"])


def last_text(line, mapping: dict) -> str | None:
    parts = []
    phs = _placeholders(line)
    if phs:
        pairs = value_pairs(phs, mapping, limit=3)
        n = len(dict.fromkeys(phs))
        parts.append(f"{n} replaced ({names_text(pairs)})")
    elif (rep := _replaced_part([line], mapping)):
        parts.append(f"{rep} replaced")
    withheld = list(getattr(line, "withheld", None) or [])
    if withheld:
        parts.append(f"{len(withheld)} withheld -> hermie allow {withheld[0]}")
    if getattr(line, "held", None) and getattr(line, "approved_by", None) is None:
        parts.append(f"1 held, not sent -> hermie allow {line.held}")
    if getattr(line, "restored", 0):
        parts.append(f"{line.restored} put back")
    return " · ".join(parts) if parts else "nothing replaced"


def post_tool_text(n: int, tool: str, where: str) -> str:
    return f"Hermie put back {n} real value{'s' if n != 1 else ''} before this {tool}{where} ran."
