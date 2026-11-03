"""Render the review as Markdown. One small helper per section; output is always redacted."""

from __future__ import annotations

from collections import Counter

from pr_agent.models import Finding, FixProposal, Level, PolicyDecision, Verification
from pr_agent.state import ReviewState
from pr_agent.tracing import redact_text

_SEVERITIES = ["critical", "high", "medium", "low"]


def _title(state: ReviewState) -> str:
    ctx = state["ctx"]
    return f"## PR review: {ctx.repo_name} #{ctx.pr_id}\n\nHead: `{ctx.head_sha[:8]}`"


def _banners(state: ReviewState) -> str:
    unique = list(dict.fromkeys(state.get("banners", [])))  # each round may re-add a banner
    return "\n\n".join(f"> {b}" for b in unique)


def _summary(findings: list[Finding], decisions: list[PolicyDecision]) -> str:
    sev: Counter[str] = Counter(f.severity for f in findings)
    levels = Counter(d.level.label for d in decisions)
    by_sev = ", ".join(f"{sev[s]} {s}" for s in _SEVERITIES if sev[s])
    by_level = ", ".join(f"{n} {lvl}" for lvl, n in sorted(levels.items()))
    line = f"**{len(findings)} finding(s)**: {by_sev}."
    return line + (f" Fixes: {by_level}." if by_level else "")


def _tool_status(state: ReviewState) -> str:
    status = state.get("tool_status", {})
    if not status:
        return ""
    return "**Tools**\n\n" + "\n".join(f"- `{name}`: {s}" for name, s in sorted(status.items()))


def _findings_table(findings: list[Finding]) -> str:
    rows = ["| ID | Severity | Location | Rule | Message |", "|---|---|---|---|---|"]
    for f in findings:
        msg = f.message.replace("|", "\\|").replace("\n", " ")
        rows.append(f"| {f.id} | {f.severity} | `{f.file}:{f.line}` | {f.rule_id or '-'} | {msg} |")
    return "### Findings\n\n" + "\n".join(rows)


def _diff_block(fix: FixProposal | None) -> str:
    return f"```diff\n{fix.patch.rstrip()}\n```" if fix and fix.patch else ""


def _failed_checks(v: Verification | None) -> str:
    if v is None or v.passed:
        return ""
    failed = ", ".join(name for name, ok in v.checks.items() if not ok) or "unknown"
    return f"Verification failed: {failed}."


def _level_body(
    d: PolicyDecision,
    fix: FixProposal | None,
    state: ReviewState,
    applied: list[str],
    dropped: dict[str, str],
    run_id: str,
) -> list[str]:
    """The level-specific part of one fix entry."""
    fid = d.finding_id
    if d.level is Level.AUTO:
        if fid in applied:
            note = f"Applied to `autofix/{state['ctx'].pr_id}`."
        else:
            note = f"Not applied: {dropped[fid]}." if fid in dropped else "Not applied."
        return [note, _diff_block(fix)]
    if d.level is Level.SUGGEST:
        if fid in state.get("approved", []):
            note = "Approved."
        else:
            note = f"Approve with `pr-agent resume {run_id} --approve {fid}`."
        return [_diff_block(fix), note]
    if d.level is Level.COMMENT:
        return [fix.explanation if fix else ""]
    owners = " ".join(d.owners)
    return [f"No patch. Owners: {owners}" if owners else "No patch."]


def _fixes(
    state: ReviewState,
    findings: list[Finding],
    applied: list[str],
    dropped: dict[str, str],
    run_id: str,
) -> str:
    decisions = {d.finding_id: d for d in state.get("decisions", [])}
    fixes = {x.finding_id: x for x in state.get("fixes", [])}
    verifs = {v.finding_id: v for v in state.get("verifications", [])}
    entries = []
    for f in findings:
        d = decisions.get(f.id)
        if d is None:
            continue
        fix, v = fixes.get(f.id), verifs.get(f.id)
        parts = [
            f"#### {f.id}: {d.level.label}",
            f"Decided by `{d.decided_by}`" + (f": {d.reason}" if d.reason else ""),
            *_level_body(d, fix, state, applied, dropped, run_id),
            _failed_checks(v) if fix else "",
        ]
        entries.append("\n\n".join(p for p in parts if p))
    return "### Fixes\n\n" + "\n\n".join(entries) if entries else ""


def _rule_proposals(state: ReviewState) -> str:
    valid = [r for r in state.get("rule_proposals", []) if r.valid]
    if not valid:
        return ""
    lines = [
        f"- `{r.rule_id}` ({r.kind}, from {r.finding_id}): {r.note or 'verified'} "
        f"(base_hits: {r.base_hits})"
        for r in valid
    ]
    return (
        "### Prevent this next time\n\n"
        + "\n".join(lines)
        + "\n\nProposal files are in the run's `rules/` directory; apply them in your own PR."
    )


def _rounds(state: ReviewState) -> str:
    history = state.get("history", [])
    if len(history) < 2:
        return ""
    lines = [
        f"- Round {r['round']}: {len(r['findings'])} finding(s), "
        f"applied {', '.join(r['applied']) or 'nothing'}"
        for r in history
    ]
    return "### Rounds\n\n" + "\n".join(lines)


def _needs_input(state: ReviewState) -> str:
    """Everything the agent could not resolve safely, with who should act and why."""
    items = state.get("escalations", [])
    if not items:
        return ""
    replies = state.get("escalation_replies", {})
    out = ["### Needs your input"]
    for e in items:
        who = " ".join(e.to)
        label = "Question for" if e.kind == "question" else "Review requested from"
        block = [f"- **{e.finding_id}** · {label} {who}", f"  - Why: {e.reason}"]
        if e.question:
            block.append(f"  - Question: {e.question}")
        block.append(f"  - Tried: {e.tried}")
        if e.evidence:
            block.append(
                "  - Evidence:\n\n    ```\n    " + e.evidence.replace("\n", "\n    ") + "\n    ```"
            )
        if e.finding_id in replies:
            block.append(f"  - Answer: {replies[e.finding_id]}")
        out.append("\n".join(block))
    return "\n\n".join(out)


def render_review(
    state: ReviewState,
    *,
    applied: list[str] | None = None,
    dropped: dict[str, str] | None = None,
    run_id: str = "<run-id>",
) -> str:
    """Full review body, redacted."""
    findings = state.get("final_findings", [])
    head = [_title(state), _banners(state)]
    if not findings:
        sections = [*head, _tool_status(state), "No issues found.", _rounds(state)]
    else:
        sections = [
            *head,
            _summary(findings, state.get("decisions", [])),
            _tool_status(state),
            _findings_table(findings),
            _fixes(state, findings, applied or [], dropped or {}, run_id),
            _needs_input(state),
            _rounds(state),
            _rule_proposals(state),
        ]
    return redact_text("\n\n".join(s for s in sections if s) + "\n")
