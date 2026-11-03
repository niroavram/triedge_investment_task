"""Convergence loop: stop conditions, stable ids, merged view, escalation classification."""

from __future__ import annotations

from typing import Any

from pr_agent.models import Finding, FixProposal, Level, PolicyDecision, Verification
from pr_agent.nodes.aggregate import assign_ids
from pr_agent.nodes.converge import route_converge
from pr_agent.nodes.escalate import classify
from pr_agent.rounds import merge_rounds, open_findings


def _f(fid: str = "", file: str = "a.py", rule: str | None = "ruff:F401", **kw: Any) -> Finding:
    defaults: dict[str, Any] = {"source": "ruff", "category": "lint", "message": "m", "line": 3}
    return Finding(id=fid, file=file, rule_id=rule, **(defaults | kw))


def _round(n: int, findings: list[Finding], applied: list[str], **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "round": n,
        "head_sha": f"sha{n}",
        "findings": findings,
        "decisions": [],
        "fixes": [],
        "verifications": [],
        "approved": [],
        "applied": applied,
        "dropped": {},
    }
    return base | kw


def test_stop_when_round_found_nothing() -> None:
    assert route_converge({"history": [_round(1, [], [])], "round": 1}) == "finish"


def test_stop_when_no_progress() -> None:
    state = {"history": [_round(1, [_f("F1")], [])], "round": 1, "max_rounds": 3}
    assert route_converge(state) == "finish"


def test_stop_at_max_rounds() -> None:
    state = {"history": [_round(3, [_f("F1")], ["F1"])], "round": 3, "max_rounds": 3}
    assert route_converge(state) == "finish"


def test_loop_when_fixes_landed_and_budget_left() -> None:
    state = {"history": [_round(1, [_f("F1")], ["F1"])], "round": 1, "max_rounds": 3}
    assert route_converge(state) == "next_round"


def test_ids_are_stable_across_rounds_and_new_findings_get_new_numbers() -> None:
    r1, ids = assign_ids([_f(line=3), _f(rule="ruff:UP045", line=9)], {})
    assert [f.id for f in r1] == ["F1", "F2"]
    # Round 2: line numbers shifted, F401 is still there, UP045 is gone, a new I001 appeared.
    r2, ids2 = assign_ids([_f(line=2), _f(rule="ruff:I001", line=1)], ids)
    assert [f.id for f in r2] == ["F1", "F3"]
    assert ids2[next(k for k in ids2 if "ruff:I001" in k)] == "F3"


def test_merge_rounds_latest_record_wins_and_tracks_resolution() -> None:
    history = [
        _round(1, [_f("F1"), _f("F2", rule="ruff:UP045")], ["F2"]),
        _round(2, [_f("F1"), _f("F3", rule="ruff:I001")], ["F3"]),
    ]
    view = merge_rounds(history)
    assert [f.id for f in view.findings] == ["F1", "F2", "F3"]
    assert view.applied == ["F2", "F3"] and view.resolved_in_round == {"F2": 1, "F3": 2}
    assert [f.id for f in open_findings(view)] == ["F1"]  # reported by round 2, never landed


def _decision(level: Level, decided_by: str, owners: list[str] | None = None) -> PolicyDecision:
    return PolicyDecision(finding_id="F1", level=level, decided_by=decided_by, owners=owners or [])


def _fix(verdict: str = "accept", notes: str = "") -> FixProposal:
    return FixProposal(
        finding_id="F1",
        patch="p",
        fix_type="bug",
        critic_verdict=verdict,  # type: ignore[arg-type]
        critic_notes=notes,
        attempts=3,
    )


def test_policy_block_becomes_review_request_to_owners() -> None:
    d = _decision(Level.FORBIDDEN, 'paths[0] "src/payments/**" -> critical', ["@payments-team"])
    e = classify(_f("F1", file="src/payments/c.py"), d, None, None, rounds=1)
    assert e.kind == "review_request" and e.to == ["@payments-team"]
    assert "src/payments/**" in e.reason and "no fix attempted" in e.tried


def test_unapproved_verified_suggestion_asks_for_approval() -> None:
    v = Verification(finding_id="F1", passed=True)
    e = classify(_f("F1"), _decision(Level.SUGGEST, "default_sensitivity -> medium"), _fix(), v, 1)
    assert e.kind == "review_request" and "--approve F1" in e.reason and e.to == ["PR author"]


def test_failing_verification_carries_evidence() -> None:
    v = Verification(
        finding_id="F1",
        passed=False,
        checks={"applies": True, "tests_pass": False},
        log="FAILED tests/test_x.py::test_y",
    )
    e = classify(_f("F1"), _decision(Level.COMMENT, "no verified patch"), _fix(), v, 2)
    assert e.kind == "review_request" and "tests_pass" in e.evidence and "test_y" in e.evidence


def test_rejected_fixes_become_a_question_for_the_author() -> None:
    fix = _fix("reject", "changes the return type for existing callers")
    e = classify(
        _f("F1", source="llm:bug", category="bug", rule=None),
        _decision(Level.COMMENT, "no verified patch"),
        fix,
        None,
        1,
    )
    assert e.kind == "question" and e.to == ["PR author"]
    assert e.question and "changes the return type" in e.question and "a.py:3" in e.question


def test_not_fixable_by_design_is_a_review_request() -> None:
    f = _f("F1", source="pytest", category="test", rule="pytest:failed", fixable=False)
    e = classify(f, _decision(Level.COMMENT, "no verified patch"), None, None, 1)
    assert e.kind == "review_request" and "not auto-fixable" in e.reason
