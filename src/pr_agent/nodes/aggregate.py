"""Turn raw fan-out findings into the short, ranked list a human will read.

Rules, in order:
1. LLM findings below `min_confidence` are dropped; tool findings are evidence and are kept.
2. Convention findings must cite a CONV-ID defined in the base CONVENTIONS.md.
3. Duplicates (same file, within 2 lines, same kind) collapse, preferring tool findings.
4. Sort by severity, then file and line; keep at most `max_comments`; ids F1, F2, ...
"""

from __future__ import annotations

import re

from langchain_core.runnables import RunnableConfig

from pr_agent.deps import get_deps
from pr_agent.gitutil import show_file
from pr_agent.models import Finding, PRContext
from pr_agent.state import ReviewState

_SEVERITY = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_CONV_ID = re.compile(r"\bCONV-\d{3}\b")


def _is_tool(f: Finding) -> bool:
    """Deterministic tool finding (bandit, ruff, ...) rather than an LLM opinion."""
    return not f.source.startswith("llm:")


def _duplicate(a: Finding, b: Finding) -> bool:
    """Same file, at most 2 lines apart, same category (security and bug count as one)."""
    same_kind = a.category == b.category or {a.category, b.category} <= {"security", "bug"}
    return a.file == b.file and abs(a.line - b.line) <= 2 and same_kind


def _valid_convention(f: Finding, known_ids: set[str]) -> bool:
    """Convention findings must cite a CONV-ID that exists in the base CONVENTIONS.md."""
    if f.category != "convention":
        return True
    cited = (f.rule_id or "").removeprefix("conv:")
    return cited in known_ids


def aggregate(state: ReviewState, config: RunnableConfig) -> dict:
    """Filter, dedupe, rank, cap and number the raw findings (see module docstring)."""
    settings = get_deps(config).settings
    known_ids = set(_CONV_ID.findall(state.get("conventions", "")))
    raw = [
        f
        for f in state.get("findings", [])
        # The confidence threshold filters LLM opinions; tool findings are evidence.
        if (_is_tool(f) or f.confidence >= settings.min_confidence)
        and _valid_convention(f, known_ids)
    ]
    # Tool findings first so they win dedupe (deterministic evidence beats LLM opinion).
    raw.sort(key=lambda f: (not _is_tool(f), _SEVERITY.get(f.severity, 9), -f.confidence))
    kept: list[Finding] = []
    for f in raw:
        if not any(_duplicate(f, k) for k in kept):
            kept.append(f)
    kept.sort(key=lambda f: (_SEVERITY.get(f.severity, 9), f.file, f.line))
    kept = kept[: settings.max_comments]
    ctx = state.get("ctx")
    landed = {fid for r in state.get("history", []) for fid in r["applied"]}
    anchors = _line_texts(ctx, kept) if ctx else {}
    final, ids = assign_ids(kept, state.get("finding_ids") or {}, anchors, landed)
    return {"final_findings": final, "finding_ids": ids}


def _line_texts(ctx: PRContext, findings: list[Finding]) -> dict[tuple[str, int], str]:
    """The flagged source line per finding, from the current head: an anchor that survives
    line shifts between rounds (unlike the line number) and differs between two findings of
    the same rule in one file (unlike file + rule alone)."""
    out: dict[tuple[str, int], str] = {}
    for path in {f.file for f in findings}:
        lines = (show_file(ctx.repo_path, ctx.head_sha, path) or "").splitlines()
        for f in findings:
            if f.file == path and 0 < f.line <= len(lines):
                out[(path, f.line)] = " ".join(lines[f.line - 1].split())
    return out


def stable_key(f: Finding, anchor: str, ordinal: int) -> str:
    """file + rule (or source/category) + the flagged line's text + ordinal among equals."""
    return f"{f.file}|{f.rule_id or f.source + ':' + f.category}|{anchor}|{ordinal}"


def assign_ids(
    findings: list[Finding],
    known: dict[str, str],
    anchors: dict[tuple[str, int], str] | None = None,
    landed: set[str] | None = None,
) -> tuple[list[Finding], dict[str, str]]:
    """Reuse F<n> for a finding seen in an earlier round; new findings get the next number.

    An id whose fix already landed is never handed out again: a still-open finding must not
    inherit a resolved one's id (it would be shown as fixed and never escalated).
    """
    ids = dict(known)
    anchors, landed = anchors or {}, landed or set()
    next_no = 1 + max((int(v.lstrip("F")) for v in ids.values()), default=0)
    seen: dict[str, int] = {}
    out = []
    for f in findings:
        base = stable_key(f, anchors.get((f.file, f.line), ""), 0)
        seen[base] = seen.get(base, 0) + 1
        key = stable_key(f, anchors.get((f.file, f.line), ""), seen[base])
        if key not in ids or ids[key] in landed:
            ids[key] = f"F{next_no}"
            next_no += 1
        out.append(f.model_copy(update={"id": ids[key]}))
    return out, ids
