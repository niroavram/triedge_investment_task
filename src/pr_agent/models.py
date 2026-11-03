"""Shared data contracts. Every node reads and writes these models only."""

from __future__ import annotations

from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, Field


class Level(IntEnum):
    """Autonomy level. Higher = more restrictive; combine with max()."""

    AUTO = 0  # fix, verify, commit to autofix/<pr>
    SUGGEST = 1  # suggestion block, needs approval
    COMMENT = 2  # explain the problem, no patch
    FORBIDDEN = 3  # no patch, tag owners

    @classmethod
    def parse(cls, name: str) -> Level:
        return cls[name.upper()]

    @property
    def label(self) -> str:
        return self.name.lower()


Severity = Literal["low", "medium", "high", "critical"]
Category = Literal["security", "bug", "lint", "format", "convention", "test", "config"]
FixType = Literal["format", "lint", "typo", "bug", "security", "public_api_change"]


class Finding(BaseModel):
    id: str = ""  # assigned by aggregate: F1, F2, ...
    source: str  # "bandit", "ruff", "semgrep", "gitleaks", "pytest", "llm:security", ...
    rule_id: str | None = None  # "bandit:B608", "ruff:F401", "custom:SEC-1", "conv:CONV-012"
    category: Category
    severity: Severity = "medium"
    file: str
    line: int = 1
    end_line: int | None = None
    message: str
    confidence: float = 1.0  # tools: 1.0; LLM: self-reported
    fixable: bool = True


class FixProposal(BaseModel):
    finding_id: str
    patch: str  # unified diff, paths relative to repo root (a/ b/ prefixes)
    explanation: str = ""
    fix_type: FixType  # derived from the finding source, never chosen by the LLM
    author: Literal["deterministic", "llm"] = "llm"
    attempts: int = 1
    critic_verdict: Literal["accept", "revise", "reject"] | None = None
    critic_notes: str = ""


class Verification(BaseModel):
    finding_id: str
    passed: bool
    checks: dict[str, bool] = Field(default_factory=dict)  # "applies", "finding_gone", ...
    log: str = ""


class PolicyDecision(BaseModel):
    finding_id: str
    level: Level
    decided_by: str  # e.g. 'paths[0] "src/payments/**" -> critical' or 'hard_rule:no_test_skip'
    reason: str = ""
    owners: list[str] = Field(default_factory=list)


class ChangedFile(BaseModel):
    path: str
    status: Literal["added", "modified", "deleted", "renamed"] = "modified"
    old_path: str | None = None
    added_lines: list[int] = Field(default_factory=list)  # line numbers in head
    patch: str = ""  # this file's hunk text


class PRContext(BaseModel):
    repo_path: str  # local git checkout containing both base and head
    repo_name: str  # "owner/name" or directory name
    pr_id: str  # "123" for GitHub, branch name for --local
    base_sha: str
    head_sha: str
    head_ref: str = ""
    title: str = ""
    description: str = ""
    files: list[ChangedFile] = Field(default_factory=list)
    diff: str = ""
    mode: Literal["local", "github"] = "local"


class RuleProposal(BaseModel):
    finding_id: str
    kind: Literal["enable_rule", "semgrep_rule", "convention"]
    rule_id: str  # "ruff:S608", "custom:SEC-2", "CONV-007"
    content: str  # rule yaml / enable snippet / convention markdown
    test_content: str | None = None  # semgrep test file
    valid: bool = False
    base_hits: int = 0
    note: str = ""


def fix_type_for(f: Finding) -> FixType:
    """Fix type comes from where the finding came from, never from the LLM's opinion."""
    if f.category == "format" and f.source == "ruff":
        return "format"  # only the formatter's own finding; an LLM "format" claim is a bug
    if f.source == "ruff" or (f.rule_id or "").startswith(("custom:", "conv:")):
        return "lint"  # repo lint rules (ruff, custom semgrep) and CONV-IDs -> lint_rules caps
    if f.category == "convention" or f.source == "llm:conventions":
        return "lint"
    if f.category == "security" or f.source in {"bandit", "gitleaks", "llm:security"}:
        return "security"
    return "bug"


class Escalation(BaseModel):
    """A finding the agent could not resolve safely, handed to a person."""

    finding_id: str
    kind: Literal["review_request", "question"]
    to: list[str] = Field(default_factory=list)  # owners, or ["PR author"]
    reason: str  # why the agent stopped, e.g. the policy rule that decided it
    tried: str = ""  # what was attempted (rounds, fixes, critic verdicts)
    evidence: str = ""  # failed checks / log tail
    question: str | None = None  # concrete question for kind == "question"
