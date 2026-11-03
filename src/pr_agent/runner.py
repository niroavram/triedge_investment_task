"""Run, pause at the approval gate, and resume reviews. The CLI and evals both use this."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langgraph.types import Command

from pr_agent.deps import Deps
from pr_agent.graph import build_graph, sqlite_checkpointer
from pr_agent.models import PRContext
from pr_agent.tracing import make_tracer, run_config

# Called with the pending `suggest` fixes; returns the finding ids to approve.
Approver = Callable[[list[dict[str, Any]]], list[str]]
# Called with the escalations; returns {finding_id: answer} (non-interactive: {}).
Responder = Callable[[list[dict[str, Any]]], dict[str, str]]


@dataclass
class RunResult:
    run_id: str
    state: dict[str, Any]
    trace_url: str | None


def _approve_none(pending: list[dict[str, Any]]) -> list[str]:
    return []


def _answer_none(items: list[dict[str, Any]]) -> dict[str, str]:
    return {}


def _drive(
    graph: Any, first_input: Any, cfg: dict, approver: Approver, responder: Responder
) -> dict[str, Any]:
    """Invoke; on each pause (approval gate or escalation) ask a human callback and resume."""
    result = graph.invoke(first_input, cfg)
    while result.get("__interrupt__"):
        value = result["__interrupt__"][0].value
        if "escalations" in value:
            # A dict resume value means {interrupt_id: value} to LangGraph, and an empty one
            # resumes nothing (the interrupt fires again forever), so send a list of pairs.
            answer: Any = list(responder(value["escalations"]).items())
        else:
            answer = approver(value.get("suggest", []))
        result = graph.invoke(Command(resume=answer), cfg)
    return result


def _trace_url(tracer: Any) -> str | None:
    try:
        return tracer.get_run_url() if tracer else None
    except Exception:  # noqa: BLE001 - a missing URL must never fail a review
        return None


def run_review(
    ctx: PRContext,
    deps: Deps,
    approver: Approver = _approve_none,
    banners: list[str] | None = None,
    responder: Responder = _answer_none,
) -> RunResult:
    run_id = f"{ctx.pr_id}-{ctx.head_sha[:8]}-{time.strftime('%Y%m%d%H%M%S')}"
    output_dir = deps.settings.work_dir / "runs" / run_id
    tracer = make_tracer()
    cfg = run_config(ctx, run_id, deps, tracer)
    checkpointer = sqlite_checkpointer(deps.settings.work_dir / "checkpoints.sqlite")
    graph = build_graph(checkpointer, converge=deps.settings.converge)
    initial = {
        "ctx": ctx,
        "output_dir": str(output_dir),
        "banners": banners or [],
        "round": 1,
        "max_rounds": deps.settings.max_rounds,
    }
    state = _drive(graph, initial, cfg, approver, responder)
    return RunResult(run_id, state, _trace_url(tracer))


def resume_review(run_id: str, deps: Deps, approve: list[str]) -> RunResult:
    """`/autofix apply` equivalent: re-run the gate of a finished run with new approvals.

    Time-travels to the checkpoint just before policy_gate, so verification results are
    reused and nothing upstream (LLM calls, analyzers) runs again.
    """
    checkpointer = sqlite_checkpointer(deps.settings.work_dir / "checkpoints.sqlite")
    graph = build_graph(checkpointer, converge=deps.settings.converge)
    thread = {"configurable": {"thread_id": run_id, "deps": deps}}
    before_gate = next(
        (s for s in graph.get_state_history(thread) if s.next == ("policy_gate",)), None
    )
    if before_gate is None:
        raise ValueError(f"run {run_id} not found or never reached the approval gate")
    ctx = before_gate.values["ctx"]
    tracer = make_tracer()
    cfg = run_config(ctx, run_id, deps, tracer)
    cfg["configurable"]["checkpoint_id"] = before_gate.config["configurable"]["checkpoint_id"]
    first = graph.invoke(None, cfg)
    cfg["configurable"].pop("checkpoint_id")
    pending = first.get("__interrupt__")
    if not pending or "suggest" not in pending[0].value:
        raise ValueError(f"run {run_id} has no fixes awaiting approval at its latest gate")
    offered = {p["id"] for p in pending[0].value["suggest"]}
    unknown = sorted(set(approve) - offered)
    if unknown:
        raise ValueError(
            f"not awaiting approval: {', '.join(unknown)} (offered: {sorted(offered)})"
        )
    result = _drive(graph, Command(resume=approve), cfg, _approve_none, _answer_none)
    return RunResult(run_id, result, _trace_url(tracer))
