"""The review graph.

ingest -> triage -> [Send] static analysis + tests + LLM reviewers -> aggregate
       -> [Send per fixable finding] fix_pipeline (propose -> critic (<=2 revisions) -> verify)
       -> policy_gate (deterministic; interrupt() for `suggest`) -> apply_fixes
       -> converge: next_round (back to triage on the autofix tip) | escalate
       -> learn_rules ("prevent this next time", non-blocking) -> publish
"""

from __future__ import annotations

import operator
import sqlite3
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from pr_agent.agents.reviewers import ReviewerKind
from pr_agent.deps import get_deps
from pr_agent.models import FixProposal, Level, Verification
from pr_agent.nodes import fix
from pr_agent.nodes.aggregate import aggregate
from pr_agent.nodes.analyze import static_analysis
from pr_agent.nodes.apply import apply_fixes
from pr_agent.nodes.converge import next_round, route_converge
from pr_agent.nodes.escalate import escalate
from pr_agent.nodes.gate import policy_gate
from pr_agent.nodes.ingest import ingest
from pr_agent.nodes.learn import learn_rules
from pr_agent.nodes.publish import publish
from pr_agent.nodes.review import make_reviewer_node
from pr_agent.nodes.tests import test_runner
from pr_agent.nodes.triage import triage
from pr_agent.nodes.verify import verify_fix
from pr_agent.policy.config import AutonomyConfig
from pr_agent.policy.engine import pre_ceiling
from pr_agent.state import ReviewState

REVIEWERS: tuple[ReviewerKind, ...] = ("security", "bug", "conventions")
ANALYSIS_NODES = ["static_analysis", "test_runner"]


class FixOutput(TypedDict):
    """Only these keys flow from a fix pipeline back to the parent (they have reducers)."""

    fixes: Annotated[list[FixProposal], operator.add]
    verifications: Annotated[list[Verification], operator.add]


def build_fix_pipeline():  # type: ignore[no-untyped-def]
    g = StateGraph(fix.FixState, output_schema=FixOutput)
    g.add_node("propose", fix.propose)
    g.add_node("critic", fix.critic)
    g.add_node("verify", verify_fix)
    g.add_node("done", fix.done)
    g.add_edge(START, "propose")
    g.add_conditional_edges(  # deterministic (tool) fixes skip the LLM critic
        "propose", fix.route_after_propose, {"critic": "critic", "verify": "verify", "done": "done"}
    )
    g.add_conditional_edges(
        "critic", fix.route_after_critic, {"propose": "propose", "verify": "verify", "done": "done"}
    )
    g.add_edge("verify", END)
    g.add_edge("done", END)
    return g.compile()


def fan_out_analysis(state: ReviewState) -> list[Send]:
    """Deterministic analyzers always run; LLM reviewers only if triage selected them."""
    chosen = state.get("triage", {}).get("reviewers", list(REVIEWERS))
    sends = [Send(node, state) for node in ANALYSIS_NODES]
    return sends + [Send(f"{kind}_review", state) for kind in REVIEWERS if kind in chosen]


def fan_out_fixes(state: ReviewState, config: RunnableConfig) -> list[Send] | str:
    """One fix pipeline per fixable finding whose location could ever receive a patch."""
    cfg = AutonomyConfig.model_validate(state["config"]) if state.get("config") else None
    settings = get_deps(config).settings
    candidates = [
        f
        for f in state.get("final_findings", [])
        if f.fixable and pre_ceiling(f, cfg, state["ctx"]) <= Level.SUGGEST
    ][: settings.max_fixes]
    if not candidates:
        return "policy_gate"
    return [
        Send(
            "fix_pipeline",
            {
                "finding": f,
                "ctx": state["ctx"],
                "config": state.get("config"),
                "conventions": state.get("conventions", ""),
                "attempts": 0,
                "max_revisions": settings.max_revisions,
                # new lint a fix introduces may wait for the next round only if one exists
                "can_defer": settings.converge
                and state.get("round", 1) < state.get("max_rounds", settings.max_rounds),
            },
        )
        for f in candidates
    ]


def build_graph(  # type: ignore[no-untyped-def]
    checkpointer: BaseCheckpointSaver | None = None, converge: bool = False
):
    g = StateGraph(ReviewState)
    g.add_node("ingest", ingest)
    g.add_node("triage", triage)
    g.add_node("static_analysis", static_analysis)
    g.add_node("test_runner", test_runner)
    for kind in REVIEWERS:
        g.add_node(f"{kind}_review", make_reviewer_node(kind))  # type: ignore[arg-type]
    g.add_node("aggregate", aggregate)
    g.add_node("fix_pipeline", build_fix_pipeline())
    g.add_node("policy_gate", policy_gate)
    g.add_node("apply_fixes", apply_fixes)
    if converge:
        g.add_node("next_round", next_round)
        g.add_node("escalate", escalate)
    g.add_node("learn_rules", learn_rules)  # non-blocking: failures become a skipped status
    g.add_node("publish", publish)

    g.add_edge(START, "ingest")
    g.add_edge("ingest", "triage")
    fan_targets = ANALYSIS_NODES + [f"{k}_review" for k in REVIEWERS]
    g.add_conditional_edges("triage", fan_out_analysis, fan_targets)
    for node in fan_targets:
        g.add_edge(node, "aggregate")
    g.add_conditional_edges("aggregate", fan_out_fixes, ["fix_pipeline", "policy_gate"])
    g.add_edge("fix_pipeline", "policy_gate")
    g.add_edge("policy_gate", "apply_fixes")
    # Outer convergence loop: re-analyze the autofix tip until done, stuck, or out of rounds.
    if converge:  # PR_AGENT_CONVERGE=1 (experimental, off by default)
        g.add_conditional_edges(
            "apply_fixes", route_converge, {"next_round": "next_round", "finish": "escalate"}
        )
        g.add_edge("next_round", "triage")
        g.add_edge("escalate", "learn_rules")
    else:
        g.add_edge("apply_fixes", "learn_rules")
    g.add_edge("learn_rules", "publish")
    g.add_edge("publish", END)
    return g.compile(checkpointer=checkpointer)


def serializer() -> JsonPlusSerializer:
    """Allow our own pydantic models to round-trip through the checkpointer."""
    return JsonPlusSerializer(
        allowed_msgpack_modules=[
            ("pr_agent.models", name)
            for name in (
                "Finding",
                "FixProposal",
                "Verification",
                "PolicyDecision",
                "Level",
                "ChangedFile",
                "PRContext",
                "RuleProposal",
                "Escalation",
            )
        ]
    )


def sqlite_checkpointer(path: Path) -> BaseCheckpointSaver:
    from langgraph.checkpoint.sqlite import SqliteSaver

    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False), serde=serializer())
