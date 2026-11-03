"""LLM fixer: asks for exact search/replace edits and builds the diff itself."""

from __future__ import annotations

import difflib

from pydantic import BaseModel

from pr_agent import gitutil
from pr_agent.agents.llm import UNTRUSTED_NOTICE, describe_finding, load_prompt, untrusted
from pr_agent.deps import LLM
from pr_agent.models import Finding, FixProposal, PRContext, fix_type_for

_NO_EOL = "\\ No newline at end of file\n"


class Edit(BaseModel):
    """Replace one exact snippet of `file` with `new`."""

    file: str
    old: str
    new: str


class FixOutput(BaseModel):
    """Structured fixer output."""

    edits: list[Edit] = []
    explanation: str = ""


def make_unified_diff(path: str, before: str, after: str) -> str:
    """A `git apply`-able unified diff for one file ("" when nothing changed)."""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    # A last line without "\n" needs git's no-newline marker after it.
    return "".join(line if line.endswith("\n") else f"{line}\n{_NO_EOL}" for line in lines)


def apply_edits(files: dict[str, str], edits: list[Edit]) -> dict[str, str] | None:
    """Apply edits in memory; None if any `old` is empty, missing or not unique."""
    out = dict(files)
    for e in edits:
        content = out.get(e.file)
        if content is None or not e.old or content.count(e.old) != 1:
            return None
        out[e.file] = content.replace(e.old, e.new, 1)
    return out


def _user_message(finding: Finding, files: dict[str, str], boundaries: str, feedback: str) -> str:
    """Finding, current file content (untrusted) and optional limits/feedback."""
    parts = ["## Finding", describe_finding(finding)]
    for path, content in files.items():
        # The path is PR-controlled, so it only appears inside the wrapper's label.
        parts += ["## Current file content", untrusted(f"file:{path}", content)]
    if boundaries:
        parts += ["## Autonomy boundaries (context)", boundaries]
    if feedback:
        # critic notes are LLM output over PR content: second-order untrusted data
        parts += ["## Reviewer feedback on your previous attempt", untrusted("critic", feedback)]
    return "\n\n".join(parts)


def propose_fix(
    finding: Finding,
    ctx: PRContext,
    llm: LLM,
    *,
    boundaries: str = "",
    feedback: str = "",
) -> FixProposal | None:
    """Ask the LLM for edits and turn them into a FixProposal, or None if unusable."""
    head = gitutil.show_file(ctx.repo_path, ctx.head_sha, finding.file)
    if head is None:
        return None
    system = f"{load_prompt('fixer')}\n\n{UNTRUSTED_NOTICE}"
    user = _user_message(finding, {finding.file: head}, boundaries, feedback)
    out = llm.structured(FixOutput, system, user, role="fix")
    if not out.edits:
        return None

    originals: dict[str, str] = {finding.file: head}
    for path in {e.file for e in out.edits} - originals.keys():
        content = gitutil.show_file(ctx.repo_path, ctx.head_sha, path)
        if content is None:
            return None
        originals[path] = content

    edited = apply_edits(originals, out.edits)
    if edited is None:
        return None
    patch = "".join(
        make_unified_diff(path, originals[path], edited[path]) for path in sorted(originals)
    )
    if not patch:
        return None
    return FixProposal(
        finding_id=finding.id,
        patch=patch,
        explanation=out.explanation,
        fix_type=fix_type_for(finding),
        author="llm",
    )
