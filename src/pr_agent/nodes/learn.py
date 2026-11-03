"""learn_rules: turn verified findings into "prevent this next time" proposals. Non-blocking.

Ladder per finding, first valid option wins: (a) enable an existing ruff/bandit rule,
(b) a new semgrep rule, (c) a CONVENTIONS.md entry. Findings on critical/high paths had no
fix attempted, so they only get (a), labelled partially verified. Proposals never touch the
PR or autofix/<pr>; they are written under <run dir>/rules/ for a human to apply.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

from pr_agent.agents import rule_drafter
from pr_agent.deps import Deps, get_deps
from pr_agent.gitutil import apply_patch, materialize, show_file
from pr_agent.learning import existing_rules, verify_rule
from pr_agent.models import Finding, FixProposal, Level, PRContext, RuleProposal
from pr_agent.policy.config import AutonomyConfig
from pr_agent.policy.engine import pre_ceiling
from pr_agent.rounds import merge_rounds
from pr_agent.state import ReviewState
from pr_agent.tracing import redact_text

STATUS_KEY = "rule_learning"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


@dataclass
class Run:
    """Per-run context shared by the ladder steps."""

    deps: Deps
    ctx: PRContext
    base_dir: Path
    selection: tuple[list[str], list[str]]
    semgrep_ids: list[str]
    conventions: str
    log: list[str] = field(default_factory=list)
    temp_dirs: list[Path] = field(default_factory=list)

    def checkout(self, sha: str, patch: str | None = None) -> Path | None:
        """Materialized `sha` (+ patch). None when the patch no longer applies."""
        path = materialize(self.ctx.repo_path, sha)
        self.temp_dirs.append(path)
        if patch is not None and not apply_patch(path, patch)[0]:
            return None
        return path


def _round_from_state(state: ReviewState) -> dict[str, Any]:
    """The single-round record used when the graph ran without the convergence loop."""
    return {
        "round": 1,
        "head_sha": state["ctx"].head_sha,
        "findings": state.get("final_findings", []),
        "decisions": state.get("decisions", []),
        "fixes": state.get("fixes", []),
        "verifications": state.get("verifications", []),
        "approved": state.get("approved") or [],
        "applied": [],
        "dropped": {},
    }


def _fix_heads(history: list[dict[str, Any]], default: str) -> dict[str, str]:
    """finding id -> the head sha its (latest) fix was written against."""
    heads: dict[str, str] = {}
    for record in history:
        for fix in record["fixes"]:
            heads[fix.finding_id] = record.get("head_sha") or default
    return heads


def _step_existing(
    run: Run, finding: Finding, head: Path, fixed: Path | None, note: str = ""
) -> RuleProposal | None:
    """Ladder (a): the best not-yet-enabled rule that passes the deterministic checks."""
    for hit in existing_rules.candidates(run.deps.sandbox, head, finding, run.selection):
        proposal = RuleProposal(
            finding_id=finding.id,
            kind="enable_rule",
            rule_id=hit.rule_id,
            content=existing_rules.enable_snippet(hit.rule_id),
            note=note,
        )
        checked = verify_rule.verify_existing(
            proposal, finding, run.deps.sandbox, head, fixed, run.base_dir
        )
        if checked.valid:
            return checked
        run.log.append(f"{finding.id} {hit.rule_id} {checked.note}")
    return None


def _step_drafts(run: Run, finding: Finding, head: Path, fixed: Path) -> RuleProposal | None:
    """Ladder (b) then (c): LLM drafts, validated deterministically."""
    before_src = (head / finding.file).read_text()
    after_src = (fixed / finding.file).read_text() if (fixed / finding.file).is_file() else ""
    before, after = rule_drafter.before_after(before_src, after_src, finding.line)
    llm, sandbox = run.deps.llm, run.deps.sandbox
    try:
        rule = rule_drafter.draft_semgrep_rule(llm, finding, before, after, run.semgrep_ids)
        rule = verify_rule.verify_semgrep(rule, finding, sandbox, head, fixed, run.base_dir)
    except ValueError as e:
        rule = None
        run.log.append(f"{finding.id} semgrep_rule invalid: {e}")
    if rule is not None and rule.valid:
        run.semgrep_ids.append(verify_rule.bare_id(rule.rule_id))
        return rule
    if rule is not None:
        run.log.append(f"{finding.id} {rule.rule_id} {rule.note}")
    conv, draft = rule_drafter.draft_convention(llm, finding, before, after, run.conventions)
    conv = verify_rule.verify_convention(
        conv, draft.bad_example, draft.good_example, rule_drafter.conv_ids(run.conventions)
    )
    if conv.valid:
        run.conventions += f"\n{conv.content}"  # so a second convention gets the next id
        return conv
    run.log.append(f"{finding.id} {conv.rule_id} {conv.note}")
    return None


def _propose(
    run: Run,
    finding: Finding,
    fix: FixProposal | None,
    verified: bool,
    critical: bool,
    head_sha: str,
) -> RuleProposal | None:
    if verified and fix is not None:
        head = run.checkout(head_sha)
        fixed = run.checkout(head_sha, fix.patch)
        if head is None or fixed is None or not (head / finding.file).is_file():
            run.log.append(f"{finding.id}: verified patch no longer applies")
            return None
        return _step_existing(run, finding, head, fixed) or _step_drafts(run, finding, head, fixed)
    if critical:
        head = run.checkout(run.ctx.head_sha)
        if head is None or not (head / finding.file).is_file():
            return None
        return _step_existing(run, finding, head, None, verify_rule.PARTIAL_NOTE)
    return None


def _learn(state: ReviewState, deps: Deps) -> tuple[list[RuleProposal], list[str]]:
    """Proposals (at most settings.max_rule_proposals) plus a log of what was dropped."""
    ctx = state["ctx"]
    history = state.get("history") or [_round_from_state(state)]
    view = merge_rounds(history)
    fixes = {x.finding_id: x for x in view.fixes}
    checks = {v.finding_id: v for v in view.verifications}
    heads = _fix_heads(history, ctx.head_sha)
    cfg = AutonomyConfig.model_validate(state["config"]) if state.get("config") else None
    if not view.findings:
        return [], []

    base_dir = materialize(ctx.repo_path, ctx.base_sha)
    conventions = state.get("conventions") or show_file(
        ctx.repo_path, ctx.base_sha, cfg.conventions.file if cfg else "CONVENTIONS.md"
    )
    run = Run(
        deps=deps,
        ctx=ctx,
        base_dir=base_dir,
        selection=existing_rules.ruff_selection(base_dir),
        semgrep_ids=rule_drafter.base_semgrep_ids(base_dir),
        conventions=conventions or "",
        temp_dirs=[base_dir],
    )
    proposals: list[RuleProposal] = []
    try:
        for finding in view.findings:
            if len(proposals) >= deps.settings.max_rule_proposals:
                break
            if existing_rules.is_enabled(finding.rule_id, run.selection):
                continue  # already prevented by the base config
            fix, check = fixes.get(finding.id), checks.get(finding.id)
            verified = bool(fix and fix.patch and check and check.passed)
            critical = pre_ceiling(finding, cfg, ctx) >= Level.COMMENT
            head_sha = heads.get(finding.id, ctx.head_sha)
            try:
                proposal = _propose(run, finding, fix, verified, critical, head_sha)
            except Exception as e:  # noqa: BLE001 - one bad finding must not stop the rest
                run.log.append(f"{finding.id}: error: {e}"[:300])
                continue
            if proposal and all(p.rule_id != proposal.rule_id for p in proposals):
                proposals.append(proposal)
    finally:
        for path in run.temp_dirs:
            shutil.rmtree(path, ignore_errors=True)
    return proposals, run.log


def _rules_dir(state: ReviewState, deps: Deps) -> Path:
    """<run output_dir>/rules, the same run dir publish writes to."""
    if state.get("output_dir"):
        return Path(state["output_dir"]) / "rules"
    ctx = state["ctx"]
    return deps.settings.work_dir / "runs" / f"{ctx.pr_id}-{ctx.head_sha[:8]}" / "rules"


def write_proposals(out: Path, proposals: list[RuleProposal]) -> None:
    """proposals.json plus one file per artifact (rule yaml + test, snippet, convention md)."""
    out.mkdir(parents=True, exist_ok=True)
    dump = [p.model_dump(mode="json") for p in proposals]
    (out / "proposals.json").write_text(redact_text(json.dumps(dump, indent=2)))
    for p in proposals:
        name = _SAFE_NAME.sub("_", f"{p.finding_id}-{verify_rule.bare_id(p.rule_id)}")
        if p.kind == "semgrep_rule":
            (out / f"{name}.yaml").write_text(redact_text(p.content))
            (out / f"{name}.py").write_text(redact_text(p.test_content or ""))
        elif p.kind == "enable_rule":
            (out / f"{name}.toml").write_text(redact_text(p.content))
        else:
            (out / f"{name}.md").write_text(redact_text(p.content))
    # TODO(rules-branch): optionally commit these under pr-agent-proposals/<pr>/ on a
    # rules/<pr> branch via a temporary worktree (add-only; hard rules checked on its diff).


def learn_rules(state: ReviewState, config: RunnableConfig) -> dict[str, Any]:
    """Graph node. Any error degrades to "skipped" and never affects the review."""
    try:
        deps = get_deps(config)
        proposals, log = _learn(state, deps)
        status = f"ok: {len(proposals)} proposal(s)" + (
            f"; dropped: {'; '.join(log)}" if log else ""
        )
        if proposals:
            write_proposals(_rules_dir(state, deps), proposals)
        _trace(proposals)
        return {"rule_proposals": proposals, "tool_status": {STATUS_KEY: status[:1000]}}
    except Exception as e:  # noqa: BLE001 - rule learning must never break a review
        return {"rule_proposals": [], "tool_status": {STATUS_KEY: f"skipped: {e}"[:500]}}


def _trace(proposals: list[RuleProposal]) -> None:
    """rule_proposals as LangSmith metadata; a no-op without tracing."""
    try:
        from langsmith.run_helpers import get_current_run_tree

        rt = get_current_run_tree()
        if rt is not None:
            rt.add_metadata(
                {
                    "rule_proposals": [
                        {
                            "finding": p.finding_id,
                            "kind": p.kind,
                            "rule_id": p.rule_id,
                            "base_hits": p.base_hits,
                        }
                        for p in proposals
                    ]
                }
            )
    except Exception:  # noqa: BLE001 - tracing must never break a review
        pass
