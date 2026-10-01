"""Plan models and the plan file (pure; no models, no sandbox)."""
from hermie import plan_doc
from hermie.planning import Plan, PlanStep, Question, plan_problems, question_problems


def _plan(n=2) -> Plan:
    return Plan(goal="A news reader app", decisions=["Platform: iOS"], assumptions=["SwiftUI"],
                architecture="SwiftUI app, RSS feed parser, list + detail views",
                steps=[PlanStep(title=f"Step {i}", details=f"do {i}", files=[f"f{i}.swift"],
                                acceptance=[f"builds {i}"], depends_on=[i - 1] if i > 1 else [])
                       for i in range(1, n + 1)],
                risks=["feed may be slow"], out_of_scope=["push notifications"])


def test_plan_problems_accepts_a_valid_plan():
    assert plan_problems(_plan(), max_steps=20) == []


def test_plan_problems_rejects_bad_plans():
    p = _plan(3)
    p.steps[1].acceptance = []
    p.steps[2].depends_on = [3]
    probs = plan_problems(p, max_steps=2)
    assert any("at most 2 steps" in x for x in probs)
    assert any("step 2" in x and "acceptance" in x for x in probs)
    assert any("step 3" in x and "earlier" in x for x in probs)
    assert plan_problems(Plan(goal="g", architecture="a", steps=[]), 20)


def test_question_problems():
    ok = [Question(question="Platform?", options=["iOS", "Web"])]
    assert question_problems(ok) == []
    assert question_problems([])
    assert question_problems([Question(question="x", options=["only"])])
    assert question_problems([Question(question="x", options=list("abcde"))])
    assert question_problems(ok * 5)


def test_render_has_marker_ticks_and_sections():
    md = plan_doc.render(_plan(), [True, False])
    assert md.startswith(plan_doc.MARKER + "\n")
    assert "- [x] 1. Step 1" in md and "- [ ] 2. Step 2" in md
    for section in ("## Goal", "## Decisions", "## Assumptions", "## Architecture", "## Steps", "## Risks",
                    "## Out of scope"):
        assert section in md
    assert "builds 2" in md and "f2.swift" in md and "after step 1" in md


def test_user_plan_file_is_never_ours(tmp_path):
    assert plan_doc.target(tmp_path, "PLAN.md") == tmp_path / "PLAN.md"          # no file yet
    (tmp_path / "PLAN.md").write_text("# my own notes\n")
    assert plan_doc.target(tmp_path, "PLAN.md") == tmp_path / plan_doc.FALLBACK_NAME
    assert plan_doc.existing(tmp_path, "PLAN.md") == ""                          # the user's file is never read
    plan_doc.write(tmp_path / plan_doc.FALLBACK_NAME, _plan(), [True, False])
    assert (tmp_path / "PLAN.md").read_text() == "# my own notes\n"
    assert "[x] 1. Step 1" in plan_doc.existing(tmp_path, "PLAN.md")


def test_existing_and_overwrite_of_our_own_file(tmp_path):
    plan_doc.write(tmp_path / "PLAN.md", _plan(), [False, False])
    assert plan_doc.target(tmp_path, "PLAN.md") == tmp_path / "PLAN.md"          # ours: overwrite allowed
    ex = plan_doc.existing(tmp_path, "PLAN.md")
    assert "Goal: A news reader app" in ex and "[ ] 2. Step 2" in ex and "do 1" not in ex


def test_executor_block_marks_current_step():
    block = plan_doc.executor_block(_plan(), [True, False], current=2)
    assert block.startswith("[Approved plan]")
    assert "[x] 1. Step 1" in block and "-> [ ] 2. Step 2" in block
    assert "do 2" in block and "f2.swift" in block and "do 1" not in block


def test_strip_from_diff_drops_only_the_plan_file():
    git = ("diff --git a/PLAN.md b/PLAN.md\n--- a/PLAN.md\n+++ b/PLAN.md\n@@ -1 +1 @@\n-x\n+y\n"
           "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n")
    out = plan_doc.strip_from_diff(git, "PLAN.md")
    assert "PLAN.md" not in out and "app.py" in out and "+b" in out
    # the real clone-workspace form (snapshot.diff: diff -ruN --exclude=.git, paths renamed snapshot/ workspace/)
    clone = ("diff -ruN --exclude=.git snapshot/PLAN.md workspace/PLAN.md\n--- snapshot/PLAN.md\n"
             "+++ workspace/PLAN.md\n+z\n"
             "diff -ruN --exclude=.git snapshot/m.py workspace/m.py\n+q\n"
             "diff -ruN --exclude=.git snapshot/docs/PLAN.md workspace/docs/PLAN.md\n+mine\n")
    out = plan_doc.strip_from_diff(clone, "PLAN.md")
    assert "snapshot/PLAN.md" not in out and "+z" not in out and "m.py" in out
    assert "docs/PLAN.md" in out and "+mine" in out             # the user's own docs/PLAN.md stays visible
    git2 = "diff --git a/docs/PLAN.md b/docs/PLAN.md\n+mine\n"
    assert plan_doc.strip_from_diff(git2, "PLAN.md") == git2


def test_diff_steps():
    old, new = _plan(2), _plan(3)
    new.steps[0].details = "changed"
    d = plan_doc.diff_steps(old, new)
    assert d == {"added": [3], "changed": [1], "removed": []}
    assert plan_doc.diff_steps(None, new) == {"added": [], "changed": [], "removed": []}
    assert plan_doc.diff_steps(new, old)["removed"] == ["Step 3"]
