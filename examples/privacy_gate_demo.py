"""Privacy gate demo: needs no LLM (Presidio rules layer only), finishes in seconds.

    python examples/privacy_gate_demo.py
    python examples/privacy_gate_demo.py --judge   # plus the local judge model's contextual check (needs Ollama)
"""
from __future__ import annotations

import sys

from hermie.config import Settings
from hermie.privacy import CleanText, PrivacyGate

SAMPLES = [
    "Explain the time complexity of quicksort",
    "Customer Zhang Wei, mobile 13812345678, ID card 11010519491231002X, wants retirement financial planning",
    "Send the report to finance@example.com, server address 10.0.0.12",
    "The statements for bank card 6222021234567890128 need to be organized",
    "Project Codename A goes live next week",
    "R&D plans to lay off 30% next quarter; the list is not public yet",   # contextually sensitive: rules miss it, needs the judge model
]


def main() -> None:
    use_judge = "--judge" in sys.argv
    s = Settings(custom_keywords=("Project Codename A",))
    judge = None
    if use_judge:
        from hermie.judge import OllamaJudge
        judge = OllamaJudge(s)
    gate = PrivacyGate(s, judge=judge)
    print(f"Judge model: {'on (' + s.judge_model + ')' if use_judge else 'off (rules layer only)'}\n")
    for text in SAMPLES:
        v = gate.check(text, use_judge=use_judge)
        print(f"Input: {text}")
        if v.sensitive:
            print(f"  x Contains private data: {v.reason}")
            if v.findings:
                redacted, mapping = gate.redact(text, v.findings)
                print(f"  -> Placeholder redaction: {redacted}")
                print(f"     Local mapping (never leaves): {mapping}")
                try:
                    gate.certify(redacted)
                    print("     Redacted text may go out: OK")
                except PermissionError as e:
                    print(f"     Still cannot go out after redaction: {e}")
        else:
            print("  OK No private data, may go out")
        print()

    print("Type constraint: constructing CleanText directly ->", end=" ")
    try:
        CleanText("trying to bypass the gate")
    except PermissionError as e:
        print(f"refused ({e})")


if __name__ == "__main__":
    main()
