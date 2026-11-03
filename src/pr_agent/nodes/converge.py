"""The outer loop: re-analyze the fixed code until nothing is left, nothing changes, or we hit
max_rounds. Fixes can introduce findings, so only a fresh round proves the PR got better."""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.types import Overwrite

from pr_agent.deps import get_deps
from pr_agent.gitutil import git
from pr_agent.ingest.diff import parse_diff
from pr_agent.settings import Settings
from pr_agent.state import ReviewState


def route_converge(state: ReviewState) -> Literal["next_round", "finish"]:
    last = state["history"][-1]
    if not last["findings"]:
        return "finish"  # converged: this round found nothing
    if not last["applied"]:
        return "finish"  # no progress: nothing changed, another round would repeat itself
    if state.get("round", 1) >= state.get("max_rounds", Settings().max_rounds):
        return "finish"  # budget exhausted; what's left is escalated
    return "next_round"


def next_round(state: ReviewState, config: RunnableConfig) -> dict[str, Any]:
    """Point the PR at the autofix tip (same base) and clear the per-round accumulators."""
    ctx = state["ctx"]
    tip = state.get("autofix_sha")
    if not tip:
        raise RuntimeError("next_round requires a landed fix (route_converge checks this)")
    diff = git(ctx.repo_path, "diff", ctx.base_sha, tip)
    new_ctx = ctx.model_copy(update={"head_sha": tip, "diff": diff, "files": parse_diff(diff)})
    return {
        "ctx": new_ctx,
        "round": state.get("round", 1) + 1,
        # ingest runs once, so carry its policy notices (e.g. "PR edits .pr-agent.yml")
        "findings": Overwrite([f for f in state.get("final_findings", []) if f.source == "policy"]),
        "fixes": Overwrite([]),
        "verifications": Overwrite([]),
        "final_findings": [],
        "decisions": [],
        "approved": None,
        "max_rounds": get_deps(config).settings.max_rounds,
    }
