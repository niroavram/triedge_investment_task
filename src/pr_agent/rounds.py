"""Merge the per-round records into one view: the latest record wins per finding id."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pr_agent.models import Finding, FixProposal, PolicyDecision, Verification


@dataclass
class MergedView:
    findings: list[Finding] = field(default_factory=list)
    decisions: list[PolicyDecision] = field(default_factory=list)
    fixes: list[FixProposal] = field(default_factory=list)
    verifications: list[Verification] = field(default_factory=list)
    approved: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)  # landed on autofix/<pr>, any round
    dropped: dict[str, str] = field(default_factory=dict)
    resolved_in_round: dict[str, int] = field(default_factory=dict)
    last_round_ids: set[str] = field(default_factory=set)  # ids still reported by the last round


def _latest(items: dict[str, Any], new: list[Any], key: str) -> None:
    for item in new:
        items[getattr(item, key)] = item


def merge_rounds(history: list[dict[str, Any]]) -> MergedView:
    findings: dict[str, Finding] = {}
    decisions: dict[str, PolicyDecision] = {}
    fixes: dict[str, FixProposal] = {}
    checks: dict[str, Verification] = {}
    view = MergedView()
    for record in history:
        _latest(findings, record["findings"], "id")
        _latest(decisions, record["decisions"], "finding_id")
        _latest(fixes, record["fixes"], "finding_id")
        _latest(checks, record["verifications"], "finding_id")
        for f in record["findings"]:  # latest round that reported f decides its approval
            if f.id in record["approved"] and f.id not in view.approved:
                view.approved.append(f.id)
            elif f.id not in record["approved"] and f.id in view.approved:
                view.approved.remove(f.id)
        for fid in record["applied"]:
            view.resolved_in_round[fid] = record["round"]
            if fid not in view.applied:
                view.applied.append(fid)
        view.dropped.update(record["dropped"])
    if history:
        view.last_round_ids = {f.id for f in history[-1]["findings"]}
    view.findings = sorted(findings.values(), key=lambda f: int(f.id.lstrip("F") or 0))
    view.decisions, view.fixes = list(decisions.values()), list(fixes.values())
    view.verifications = list(checks.values())
    return view


def open_findings(view: MergedView) -> list[Finding]:
    """Still reported by the last round and not landed by it: someone has to look at these."""
    return [f for f in view.findings if f.id in view.last_round_ids and f.id not in view.applied]
