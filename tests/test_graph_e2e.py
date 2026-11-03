"""Full graph on seeded case 01 with a fake LLM: real git, real bandit, real pytest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pr_agent.agents.critic import CriticOutput
from pr_agent.agents.fixer import Edit, FixOutput
from pr_agent.agents.reviewers import LLMFinding, ReviewOutput
from pr_agent.deps import Deps
from pr_agent.gitutil import git, show_file
from pr_agent.ingest.local import load_local
from pr_agent.runner import resume_review, run_review
from pr_agent.sandbox import LocalSandbox
from pr_agent.settings import Settings
from tests.fakes import FakeLLM

BAD = "    query = f\"SELECT id, total_cents FROM orders WHERE customer = '{customer}'\"\n"
BAD += "    return conn.execute(query).fetchall()"
GOOD = '    query = "SELECT id, total_cents FROM orders WHERE customer = ?"\n'
GOOD += "    return conn.execute(query, (customer,)).fetchall()"


def _llm() -> FakeLLM:
    return FakeLLM(
        {
            # Like a real reviewer: reports the injection only while the f-string is in the code.
            "ReviewOutput": lambda system, user: (
                ReviewOutput(
                    findings=[
                        LLMFinding(
                            file="src/store/orders.py",
                            line=20,
                            severity="high",
                            category="security",
                            message="SQL injection: customer is interpolated into the query",
                            confidence=0.9,
                        )
                    ]
                )
                if "WHERE customer = '{customer}'" in user
                else ReviewOutput()
            ),
            "FixOutput": FixOutput(
                edits=[Edit(file="src/store/orders.py", old=BAD, new=GOOD)],
                explanation="Use a parameterized query (CONV-004).",
            ),
            "CriticOutput": CriticOutput(verdict="accept", notes="minimal and correct"),
        }
    )


def _deps(tmp_path: Path) -> Deps:
    return Deps(llm=_llm(), sandbox=LocalSandbox(), settings=Settings(work_dir=tmp_path))


def test_case01_end_to_end_with_approval(demo_repo: Path, tmp_path: Path) -> None:
    ctx = load_local(demo_repo, "main", "case/01-sql-injection")
    result = run_review(ctx, _deps(tmp_path), approver=lambda pending: [p["id"] for p in pending])
    out = Path(result.state["output_dir"])
    review = (out / "review.md").read_text()
    summary = json.loads((out / "review.json").read_text())

    # A tool wins dedupe: semgrep (high) outranks bandit B608 (medium) on the same line.
    assert [f["rule_id"] for f in summary["findings"]] == ["semgrep:sql-built-from-string"]
    assert summary["decisions"][0]["level"] == "suggest"
    assert summary["applied"] == ["F1"]
    assert "F1" in review and "sql-built-from-string" in review
    assert (out / "patches" / "F1.patch").exists()
    branch = summary["autofix_branch"]
    fixed = show_file(demo_repo, git(demo_repo, "rev-parse", branch).strip(), "src/store/orders.py")
    assert fixed and "(customer,)" in fixed
    assert git(demo_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_case01_resume_applies_later_approval(demo_repo: Path, tmp_path: Path) -> None:
    ctx = load_local(demo_repo, "main", "case/01-sql-injection")
    deps = _deps(tmp_path)
    first = run_review(ctx, deps)  # non-interactive: nothing approved
    assert (
        json.loads((Path(first.state["output_dir"]) / "review.json").read_text())["applied"] == []
    )
    calls_before = len(deps.llm.calls)  # type: ignore[attr-defined]

    second = resume_review(first.run_id, deps, ["F1"])
    summary = json.loads((Path(second.state["output_dir"]) / "review.json").read_text())
    assert summary["applied"] == ["F1"]
    # Nothing upstream re-ran on the old code: no new fixer/critic calls, and any new review
    # calls (convergence round 2) only ever saw the fixed code.
    new_calls = deps.llm.calls[calls_before:]  # type: ignore[attr-defined]
    assert all(c["schema"] == "ReviewOutput" for c in new_calls)
    assert not any("WHERE customer = '{customer}'" in c["user"] for c in new_calls)


def test_resume_rejects_ids_not_awaiting_approval(demo_repo: Path, tmp_path: Path) -> None:
    """An approval may only answer the approval gate, never another interrupt."""
    ctx = load_local(demo_repo, "main", "case/01-sql-injection")
    deps = _deps(tmp_path)
    first = run_review(ctx, deps)
    with pytest.raises(ValueError, match="F99"):
        resume_review(first.run_id, deps, ["F99"])
