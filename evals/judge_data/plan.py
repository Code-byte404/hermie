"""Generation plan: `python evals/judge_data/plan.py` writes plan/batches.jsonl and one prompt per batch."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from taxonomy import (CATEGORY_FORMS, EASY_BATCHES, EASY_NEG_KINDS, EASY_PER_BATCH, FORMS, HARD_NEG_KINDS, INDUSTRIES,  # noqa: E402
                      LENGTHS, PAIR_BATCHES, PAIR_SCENARIOS, POS_CATEGORIES, XL_PER_BATCH)

HERE = Path(__file__).resolve().parent

RULES = """Rules for every text you write:
- Everything is invented. Use invented names, companies, products and numbers. Never use real people, real companies' internal data, or real credentials.
- Phone numbers only in the form 555-01xx. Emails only at example.com or example.org. No API keys, tokens or passwords at all (a separate scanner handles those, so do not rely on them to make a text sensitive).
- English only. No Chinese or other non-Latin text.
- Write realistic material, as it would appear in a developer's project or chat. Vary tone, structure, vocabulary and detail between items. Do not reuse names, openings or phrasings across items.
- Do not mention the words "sensitive", "private", "confidential" or "should not be shared" unless a real document of that kind would. The text must be judged on its content, not on a label.
- Length: stay within the character range given for the batch (count roughly; going a little over or under is fine, going far outside is not).
"""

PAIR_TASK = """You are writing training data for a classifier that decides whether a piece of text contains private
information about a specific person, or non-public company information, that should not be sent to an outside AI service.

Batch {batch_id}: write {n} scenarios. Each scenario is a pair of texts of the SAME form and similar length and vocabulary:
  - "pos": a text that clearly contains {pos_desc}.
  - "hardneg": a matched text of the same form with the same vocabulary and setting, but containing nothing private or non-public:
    it discusses the topic in general, uses invented generic examples, or shows only public information. It must not be a trick:
    a careful reader should be certain it contains no private information about a specific person or non-public company information.
Both texts of a scenario belong together; make them genuinely close in wording so that topic alone cannot separate them.

Setting hint for variety: spread the {n} scenarios across settings such as {industries}.
Forms to use, rotating through them (about the same number of scenarios per form): {forms}.
Target length per text: {low}-{high} characters.

{rules}
Form descriptions: {form_desc}
"""

EASY_TASK = """You are writing training data for a classifier that decides whether a piece of text contains private
information about a specific person, or non-public company information, that should not be sent to an outside AI service.

Batch {batch_id}: write {n} texts that clearly do NOT contain any private information about a specific person or non-public
company information. Kinds to rotate through: {kinds}. Forms to rotate through: {forms}.
Target length per text: {low}-{high} characters. Make them ordinary and varied, the kind of text a coding assistant sees all day.
Where a kind is "placeholders only", use placeholders such as <PERSON_1>, <EMAIL_ADDRESS_2>, <PHONE_NUMBER_3>, <SECRET_1> in place of the values.

{rules}
Form descriptions: {form_desc}
"""

XL_TASK = """You are writing training data for a classifier that decides whether a piece of text contains private
information about a specific person, or non-public company information, that should not be sent to an outside AI service.

Batch {batch_id}: write {n} LONG texts of {low}-{high} characters each, as {n_half} pairs. Each pair has one "pos" and one "hardneg" of the
same form and overall content. The long text is mostly ordinary material (code, logs, documentation). In the "pos" text, a few lines
somewhere in the middle or end ({pos_desc}) are private or non-public; the rest is routine. The "hardneg" has the same routine
material and, in place of those lines, harmless lines of the same kind with nothing private. Put the private lines at different
positions across pairs (start, middle, last third, end).
Forms to rotate through: {forms}. Settings: {industries}.

{rules}
Form descriptions: {form_desc}
"""

OUTPUT = """How to deliver: write a Python script to {script} that defines ITEMS, a list of dicts, and writes {out} as JSON Lines
(one json.dumps(item) per line, ensure_ascii=False). Use triple-quoted strings for the texts so you never hand-escape. Run the
script. Each item has exactly these keys:
  "scenario": a short id for the scenario, unique inside this batch (e.g. "s01")
  "role": {roles}
  "form": one of the forms listed above
  "text": the text itself
Check that the output file has {total} lines, then reply with one line: the file path and the count. Do not paste the texts in your reply.
"""


def _forms_desc(forms):
    return "; ".join(f"{f} = {FORMS[f][1]}" for f in forms)


def _batches() -> list[dict]:
    out = []
    n = 0
    for cat, desc in POS_CATEGORIES.items():
        forms = CATEGORY_FORMS[cat]
        for length, count in PAIR_BATCHES.items():
            for k in range(count):
                inds = [INDUSTRIES[(n * 3 + j) % len(INDUSTRIES)] for j in range(4)]
                out.append({"id": f"pair-{cat}-{length}-{k + 1}", "kind": "pair", "category": cat, "length": length,
                            "forms": forms, "n": PAIR_SCENARIOS[length], "industries": inds, "split_only": None})
                n += 1
    n = 0
    all_forms = list(FORMS)
    for length, count in EASY_BATCHES.items():
        for k in range(count):
            kinds = list(EASY_NEG_KINDS)
            forms = [all_forms[(k + j) % len(all_forms)] for j in range(4)]
            out.append({"id": f"easy-{length}-{k + 1}", "kind": "easy", "category": "easy", "length": length,
                        "forms": forms, "n": EASY_PER_BATCH[length], "industries": [], "split_only": None})
            n += 1
    xl_cats = list(POS_CATEGORIES)
    for k in range(4):
        cats = [xl_cats[(k * 3 + j) % len(xl_cats)] for j in range(3)]
        out.append({"id": f"xl-{k + 1}", "kind": "xl", "category": "mixed", "length": "xl", "forms": ["markdown", "source_file", "shell_log", "git_diff"],
                    "n": XL_PER_BATCH, "industries": [INDUSTRIES[(k * 4 + j) % len(INDUSTRIES)] for j in range(4)],
                    "pos_categories": cats, "split_only": "test"})
    return out


def render_prompt(b: dict, raw_dir: Path) -> str:
    low, high = LENGTHS[b["length"]]
    forms = b["forms"]
    out = raw_dir / f"{b['id']}.jsonl"
    script = raw_dir / f"{b['id']}.py"
    common = dict(batch_id=b["id"], n=b["n"], low=low, high=high, rules=RULES, forms=", ".join(forms),
                  form_desc=_forms_desc(forms), industries="; ".join(b["industries"]))
    if b["kind"] == "pair":
        text = PAIR_TASK.format(pos_desc=POS_CATEGORIES[b["category"]], **common)
        roles, total = '"pos" or "hardneg"; each scenario has exactly one of each', b["n"] * 2
    elif b["kind"] == "easy":
        text = EASY_TASK.format(kinds="; ".join(f"{k} = {d}" for k, d in EASY_NEG_KINDS.items()), **common)
        roles, total = '"easyneg"', b["n"]
    else:
        descs = "; ".join(POS_CATEGORIES[c] for c in b["pos_categories"])
        text = XL_TASK.format(pos_desc=descs, n_half=b["n"] // 2, **{**common, "n": b["n"]})
        roles, total = '"pos" or "hardneg"; each scenario has exactly one of each', b["n"]
    return text + "\n" + OUTPUT.format(script=script, out=out, roles=roles, total=total)


def main(argv=None) -> int:
    plan_dir = HERE / "plan"
    raw_dir = HERE / "raw"
    (plan_dir / "prompts").mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(exist_ok=True)
    batches = _batches()
    with open(plan_dir / "batches.jsonl", "w", encoding="utf-8") as f:
        for b in batches:
            f.write(json.dumps(b) + "\n")
            (plan_dir / "prompts" / f"{b['id']}.md").write_text(render_prompt(b, raw_dir), encoding="utf-8")
    pos = sum(b["n"] for b in batches if b["kind"] == "pair")
    easy = sum(b["n"] for b in batches if b["kind"] == "easy")
    xl = sum(b["n"] for b in batches if b["kind"] == "xl")
    print(f"{len(batches)} batches: {pos} pair scenarios ({pos * 2} texts), {easy} easy negatives, {xl} xl texts; "
          f"positives about {(pos + xl // 2) / (pos * 2 + easy + xl):.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
