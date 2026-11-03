"""Schema of demo/cases/NN/expected.json (see evals/README.md)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from pr_agent.models import Category


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedFinding(_Strict):
    file: str
    category: Category
    line: int | None = None
    rule: str | None = None  # "bandit:B608" (prefix match), "CONV-002", or None
    keywords: list[str] = []  # informational: what a good message mentions


class ExpectedDecision(_Strict):
    file: str
    level: Literal["auto", "suggest", "comment", "forbidden"]
    decided_by_contains: str | None = None
    rule: str | None = None  # only decisions on findings with this rule (prefix match)


class ExpectedCase(_Strict):
    findings: list[ExpectedFinding] = []
    zero_findings: bool = False
    decisions: list[ExpectedDecision] = []
    owners_tagged: list[str] = []
    fix_verified: bool | None = None
    applied: bool = False
    config_change_flagged: bool = False
    injection: bool = False
    resolved_in_round: dict[str, int] | None = None  # {"ruff:F401": 2}, keyed by rule prefix
    escalations_expected: int | None = None
    rule_proposal: bool | None = None  # a valid rule proposal is expected (or not)
