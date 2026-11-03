"""LLM reviewers (security, bug, conventions) that turn a PR into Findings."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from pr_agent import gitutil
from pr_agent.agents.llm import UNTRUSTED_NOTICE, load_prompt, untrusted
from pr_agent.deps import LLM
from pr_agent.ingest.diff import is_reviewable
from pr_agent.models import Category, Finding, PRContext, Severity

ReviewerKind = Literal["security", "bug", "conventions"]
MAX_FILE_LINES = 400
MAX_MESSAGE_CHARS = 500

# `CONV-004`, `conv:CONV-004`, `[CONV-004]` or `[conv:CONV-004]` (any case, stray spaces).
_CONV_REF = re.compile(r"\[?\s*(?:conv\s*:\s*)?(CONV-\d{3})\s*\]?", re.IGNORECASE)


class LLMFinding(BaseModel):
    """One problem as reported by the model."""

    file: str
    line: int
    end_line: int | None = None
    severity: Severity = "medium"
    category: Category = "bug"
    message: str
    confidence: float = 0.5
    rule_id: str | None = None


class ReviewOutput(BaseModel):
    """Structured reviewer output; empty means the PR is clean."""

    findings: list[LLMFinding] = []


def _numbered(text: str, limit: int = MAX_FILE_LINES) -> str:
    """Prefix lines with head line numbers and truncate long files."""
    lines = text.splitlines()
    out = [f"{i:>4}| {line}" for i, line in enumerate(lines[:limit], start=1)]
    if len(lines) > limit:
        out.append(f"... truncated ({len(lines) - limit} more lines)")
    return "\n".join(out)


def head_files(ctx: PRContext) -> dict[str, str]:
    """HEAD text of every reviewable changed file that still exists.

    Deleted, binary/generated/vendored and non-UTF-8 files are left out, so findings on
    them are dropped by `to_findings`.
    """
    out: dict[str, str] = {}
    for f in ctx.files:
        if f.status == "deleted" or not is_reviewable(f.path):
            continue
        try:
            content = gitutil.show_file(ctx.repo_path, ctx.head_sha, f.path)
        except UnicodeDecodeError:
            continue
        if content is not None:
            out[f.path] = content
    return out


def build_user_message(
    kind: ReviewerKind, ctx: PRContext, conventions: str, files: dict[str, str] | None = None
) -> str:
    """PR metadata, diff and head files (all untrusted) plus trusted base conventions."""
    files = head_files(ctx) if files is None else files
    parts = [
        "## PR title",
        untrusted("pr_title", ctx.title),
        "## PR description",
        untrusted("pr_description", ctx.description),
        "## Unified diff",
        untrusted("diff", ctx.diff),
        "## Changed files at HEAD (line-numbered)",
    ]
    for path, content in files.items():
        if path.endswith(".py"):
            parts.append(untrusted(f"file:{path}", _numbered(content)))
    if kind == "conventions":
        parts += ["## CONVENTIONS.md (trusted, from the base branch)", conventions]
    return "\n\n".join(parts)


def _category(kind: ReviewerKind, reported: Category) -> Category:
    """Security and conventions reviewers have a fixed category."""
    if kind == "security":
        return "security"
    if kind == "conventions":
        return "convention"
    # The bug reviewer may only report bugs or security issues: a category picks the policy
    # fix type, so an injected "format"/"lint" label must not raise the autonomy ceiling.
    return reported if reported == "security" else "bug"


def normalize_conv_id(rule_id: str | None) -> str | None:
    """`CONV-004` / `conv:CONV-004` / `[CONV-004]` -> `conv:CONV-004`; anything else -> None."""
    match = _CONV_REF.fullmatch((rule_id or "").strip())
    return f"conv:{match.group(1).upper()}" if match else None


def _rule_id(kind: ReviewerKind, reported: str | None) -> str | None:
    """Conventions get a normalized CONV-ID. Other reviewers' ids are namespaced `llm:` so
    they can never impersonate a tool or repo rule (`bandit:`, `custom:`, `conv:`), which
    would change the fix type and so the autonomy level."""
    if kind == "conventions":
        return normalize_conv_id(reported)
    return f"llm:{reported.strip()}" if reported and reported.strip() else None


def _line_count(text: str) -> int:
    """Number of lines as an editor shows them (a trailing newline adds no line)."""
    return len(text.splitlines())


def to_findings(kind: ReviewerKind, out: ReviewOutput, files: dict[str, str]) -> list[Finding]:
    """Deterministic post-filter: keep only findings on a real line of a file in this PR,
    clamp the message length and confidence, and fix category and rule id by reviewer kind."""
    findings = []
    for item in out.findings:
        n_lines = _line_count(files[item.file]) if item.file in files else 0
        if not 1 <= item.line <= n_lines:
            continue  # file outside the PR, deleted, or a line that does not exist
        end_line = item.end_line
        if end_line is not None:
            end_line = min(end_line, n_lines) if end_line >= item.line else None
        findings.append(
            Finding(
                source=f"llm:{kind}",
                rule_id=_rule_id(kind, item.rule_id),
                category=_category(kind, item.category),
                severity=item.severity,
                file=item.file,
                line=item.line,
                end_line=end_line,
                message=item.message[:MAX_MESSAGE_CHARS],
                confidence=min(max(item.confidence, 0.0), 1.0),
            )
        )
    return findings


def run_reviewer(kind: ReviewerKind, ctx: PRContext, conventions: str, llm: LLM) -> list[Finding]:
    """Run one reviewer and map its output to validated Findings in files this PR changed."""
    files = head_files(ctx)
    system = f"{load_prompt(kind)}\n\n{UNTRUSTED_NOTICE}"
    user = build_user_message(kind, ctx, conventions, files)
    out = llm.structured(ReviewOutput, system, user, role=f"review:{kind}")
    return to_findings(kind, out, files)
