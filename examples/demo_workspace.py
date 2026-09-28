"""Create the demo workspace (all fictional data).

    python examples/demo_workspace.py [dir]    # default ~/HermieWork/demo
"""
from __future__ import annotations

import sys
from pathlib import Path

CLIENTS = """name,phone,city,age,assets_wan
Zhang Wei,13812345678,Shanghai,58,320
Li Na,13987654321,Beijing,34,85
Wang Qiang,13700001111,Shanghai,45,150
Liu Yang,13655556666,Shenzhen,29,40
Chen Jing,13511112222,Beijing,61,510
"""

TITLES = """How to Build a Local-First AI Agent
Understanding Differential Privacy
Ten Tips for Writing Clean Python
Why Sandboxing Matters on macOS
A Gentle Introduction to Vector Databases
"""

CALC = '''def average(nums):
    """Return the mean; an empty list returns 0."""
    return sum(nums) / len(nums) - 1   # BUG: subtracts 1 too many, and an empty list divides by zero


def median(nums):
    s = sorted(nums)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
'''

TEST_CALC = '''from calc import average, median


def test_average():
    assert average([1, 2, 3]) == 2
    assert average([]) == 0


def test_median():
    assert median([3, 1, 2]) == 2
    assert median([1, 2, 3, 4]) == 2.5
'''

MEETING = """# Weekly meeting notes (internal)

- R&D plans to lay off 30% next quarter; the list is not public yet. HR lead: Zhao Min
- The new product launch is postponed to November
- We need a communication plan for all staff that avoids the news leaking early
"""


def create(root: Path) -> Path:
    files = {
        "data/clients.csv": CLIENTS,
        "data/titles.txt": TITLES,
        "project/calc.py": CALC,
        "project/test_calc.py": TEST_CALC,
        "notes/meeting.md": MEETING,
    }
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (root / "out").mkdir(exist_ok=True)
    return root


if __name__ == "__main__":
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "~/HermieWork/demo").expanduser()
    print(f"Demo workspace created: {create(root)}")
