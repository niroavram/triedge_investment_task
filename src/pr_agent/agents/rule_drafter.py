"""rule_drafter: an LLM drafts a semgrep rule or a convention from a verified finding.

The LLM only writes text. The rule id / CONV-ID is assigned here, deterministically, and
learning/verify_rule.py decides whether the draft is valid.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml
from pydantic import BaseModel

from pr_agent.agents.llm import UNTRUSTED_NOTICE, describe_finding, load_prompt, untrusted
from pr_agent.deps import LLM
from pr_agent.models import Category, Finding, RuleProposal

ROLE = "rule"
CONTEXT_LINES = 10  # around the finding when it is not inside a function
_AREA: dict[Category, str] = {"security": "SEC", "bug": "BUG"}  # everything else: ERR
_ANNOTATION = re.compile(r"(#\s*(?:ruleid|ok|todoruleid|todook)\s*:\s*)[\w.:-]+")
_CONV = re.compile(r"\bCONV-(\d{3})\b")


class RuleDraft(BaseModel):
    rule_yaml: str = ""
    test_code: str = ""
    rationale: str = ""


class ConventionDraft(BaseModel):
    title: str = ""
    rationale: str = ""
    bad_example: str = ""
    good_example: str = ""


# --- deterministic ids ---------------------------------------------------------------------


def area_for(category: Category) -> str:
    return _AREA.get(category, "ERR")


def base_semgrep_ids(base_dir: Path | None) -> list[str]:
    """Rule ids in the base checkout's `.semgrep/*.yml|yaml` (unreadable files are skipped)."""
    folder = base_dir / ".semgrep" if base_dir else None
    if folder is None or not folder.is_dir():
        return []
    ids: list[str] = []
    for path in sorted([*folder.rglob("*.yml"), *folder.rglob("*.yaml")]):
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            continue
        ids += [str(r["id"]) for r in data.get("rules", []) if isinstance(r, dict) and "id" in r]
    return ids


def next_rule_id(area: str, existing: list[str]) -> str:
    """`<AREA>-<n>`: n is one more than the highest number already used for that area."""
    pattern = re.compile(rf"^{area}-(\d+)$")
    used = [int(m.group(1)) for i in existing if (m := pattern.match(i))]
    return f"{area}-{max(used, default=0) + 1:03d}"


def conv_ids(conventions: str) -> set[str]:
    return {f"CONV-{n}" for n in _CONV.findall(conventions)}


def next_conv_id(conventions: str) -> str:
    used = [int(n) for n in _CONV.findall(conventions)]
    return f"CONV-{max(used, default=0) + 1:03d}"


def set_rule_id(rule_yaml: str, rule_id: str) -> str:
    """Keep the first rule only and overwrite its id. Raises ValueError on unusable YAML."""
    try:
        data = yaml.safe_load(rule_yaml)
    except yaml.YAMLError as e:
        raise ValueError(f"rule yaml does not parse: {e}") from e
    rules = data.get("rules") if isinstance(data, dict) else None
    if not rules or not isinstance(rules[0], dict):
        raise ValueError("rule yaml has no rules")
    return yaml.safe_dump({"rules": [{**rules[0], "id": rule_id}]}, sort_keys=False)


def set_test_ids(test_code: str, rule_id: str) -> str:
    """Point every `# ruleid:` / `# ok:` annotation at the assigned id."""
    return _ANNOTATION.sub(lambda m: m.group(1) + rule_id, test_code)


# --- code context --------------------------------------------------------------------------


def _function_at(tree: ast.AST, line: int) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Innermost function whose body spans `line`."""
    best = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.lineno <= line <= (node.end_lineno or node.lineno):
                if best is None or node.lineno >= best.lineno:
                    best = node
    return best


def function_source(source: str, line: int, name: str | None = None) -> tuple[str, str | None]:
    """(source of the function at `line`, or named `name`; its name). Falls back to a window."""
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None
    node = None
    if tree is not None and name:
        node = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name
            ),
            None,
        )
    if tree is not None and node is None:
        node = _function_at(tree, line)
    if node is None:
        lo, hi = max(line - CONTEXT_LINES, 1), line + CONTEXT_LINES
        return "\n".join(lines[lo - 1 : hi]), None
    start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    return "\n".join(lines[start - 1 : node.end_lineno]), node.name


def before_after(before_src: str, after_src: str, line: int) -> tuple[str, str]:
    """The affected function before (head) and after (head + verified patch)."""
    before, name = function_source(before_src, line)
    after, _ = function_source(after_src, line, name)
    return before, after


# --- LLM calls -----------------------------------------------------------------------------


def build_user_message(
    kind: str, finding: Finding, before: str, after: str, existing_ids: list[str]
) -> str:
    """Finding and code are untrusted; the existing ids come from the base checkout."""
    return "\n\n".join(
        [
            f"## Draft a {kind}",
            "## Finding",
            describe_finding(finding),
            "## Affected code BEFORE the fix (contains the mistake)",
            untrusted(f"before:{finding.file}", before),
            "## Affected code AFTER the verified fix",
            untrusted(f"after:{finding.file}", after),
            "## Existing ids (trusted, from the base branch; do not duplicate)",
            ", ".join(existing_ids) or "(none)",
        ]
    )


def _system() -> str:
    return f"{load_prompt('rule_drafter')}\n\n{UNTRUSTED_NOTICE}"


def draft_semgrep_rule(
    llm: LLM, finding: Finding, before: str, after: str, existing_ids: list[str]
) -> RuleProposal:
    """Ladder step (b). The id is `<AREA>-<n>` from the base ids, whatever the LLM wrote."""
    rule_id = next_rule_id(area_for(finding.category), existing_ids)
    user = build_user_message("semgrep rule (RuleDraft)", finding, before, after, existing_ids)
    draft = llm.structured(RuleDraft, _system(), user, role=ROLE)
    return RuleProposal(
        finding_id=finding.id,
        kind="semgrep_rule",
        rule_id=f"custom:{rule_id}",
        content=set_rule_id(draft.rule_yaml, rule_id),
        test_content=set_test_ids(draft.test_code, rule_id),
        note=draft.rationale[:300],
    )


def convention_markdown(conv_id: str, draft: ConventionDraft) -> str:
    return (
        f"## {conv_id}: {draft.title.strip()}\n{draft.rationale.strip()}\n\n"
        f"Bad:\n```python\n{draft.bad_example.strip()}\n```\n\n"
        f"Good:\n```python\n{draft.good_example.strip()}\n```\n"
    )


def draft_convention(
    llm: LLM, finding: Finding, before: str, after: str, conventions: str
) -> tuple[RuleProposal, ConventionDraft]:
    """Ladder step (c): a CONVENTIONS.md entry with the next free CONV-ID."""
    conv_id = next_conv_id(conventions)
    existing = sorted(conv_ids(conventions))
    user = build_user_message("convention (ConventionDraft)", finding, before, after, existing)
    draft = llm.structured(ConventionDraft, _system(), user, role=ROLE)
    proposal = RuleProposal(
        finding_id=finding.id,
        kind="convention",
        rule_id=conv_id,
        content=convention_markdown(conv_id, draft),
    )
    return proposal, draft
