"""Escalate, never drop: every finding still open when the loop stops goes to a person."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from pr_agent.models import Escalation, Finding, FixProposal, Level, PolicyDecision, Verification
from pr_agent.rounds import merge_rounds, open_findings
from pr_agent.state import ReviewState

AUTHOR = ["PR author"]
NO_PATCH = "no verified patch"


def _failed(v: Verification) -> str:
    bad = [name for name, ok in v.checks.items() if not ok]
    return f"failed checks: {', '.join(bad)}\n{v.log[-600:]}"


def classify(
    f: Finding,
    d: PolicyDecision | None,
    fix: FixProposal | None,
    v: Verification | None,
    rounds: int,
) -> Escalation:
    """Map why the agent stopped to who should act, and what they need to know."""
    tried = f"{rounds} round(s); " + (
        f"fix by {fix.author}, {fix.attempts} attempt(s), critic: {fix.critic_verdict}"
        if fix
        else "no fix attempted"
    )
    owners = (d.owners if d else []) or AUTHOR
    if not f.fixable:
        return Escalation(
            finding_id=f.id,
            kind="review_request",
            to=owners,
            tried=tried,
            reason="not auto-fixable by design (needs a human decision)",
            evidence=f.message,
        )
    if d and d.level >= Level.COMMENT and not d.decided_by.startswith(NO_PATCH):
        return Escalation(
            finding_id=f.id,
            kind="review_request",
            to=owners,
            tried=tried,
            reason=f"blocked by policy: {d.decided_by}",
        )
    if d and d.level is Level.SUGGEST and v and v.passed:
        return Escalation(
            finding_id=f.id,
            kind="review_request",
            to=owners,
            tried=tried,
            reason=f"verified fix awaiting approval (`pr-agent resume <run-id> --approve {f.id}`)",
        )
    if fix and fix.critic_verdict != "reject" and v and not v.passed:
        return Escalation(
            finding_id=f.id,
            kind="review_request",
            to=owners,
            tried=tried,
            reason="the fix kept failing verification",
            evidence=_failed(v),
        )
    notes = f" The reviewer's objection: {fix.critic_notes}" if fix and fix.critic_notes else ""
    return Escalation(
        finding_id=f.id,
        kind="question",
        to=AUTHOR,
        tried=tried,
        reason="intent is ambiguous or every fix attempt was rejected",
        question=f"`{f.file}:{f.line}`: {f.message}.{notes} What is the intended behaviour "
        "here, so we can fix it the right way?",
    )


def escalate(state: ReviewState, config: RunnableConfig) -> dict[str, Any]:
    history = state.get("history", [])
    view = merge_rounds(history)
    decisions = {d.finding_id: d for d in view.decisions}
    fixes = {x.finding_id: x for x in view.fixes}
    checks = {v.finding_id: v for v in view.verifications}
    items = [
        classify(f, decisions.get(f.id), fixes.get(f.id), checks.get(f.id), len(history))
        for f in open_findings(view)
    ]
    _trace(len(history), items)
    replies: Any = state.get("escalation_replies")
    if items and replies is None:
        # resume value: [[finding_id, answer], ...] (a dict would be read as interrupt ids)
        pairs = interrupt({"escalations": [e.model_dump(mode="json") for e in items]})
        replies = dict(pairs or [])
    return {"escalations": items, "escalation_replies": replies or {}}


def _trace(rounds: int, items: list[Escalation]) -> None:
    """rounds + escalations as LangSmith metadata; a no-op without tracing."""
    try:
        from langsmith.run_helpers import get_current_run_tree

        rt = get_current_run_tree()
        if rt is not None:
            rt.add_metadata(
                {
                    "rounds": rounds,
                    "escalations": [
                        {"finding": e.finding_id, "kind": e.kind, "reason": e.reason} for e in items
                    ],
                }
            )
    except Exception:  # noqa: BLE001 - tracing must never break a review
        pass
