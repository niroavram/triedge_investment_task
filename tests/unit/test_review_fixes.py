"""Regression tests for the independent code review's confirmed findings."""

from __future__ import annotations

from pathlib import Path

import pytest

from pr_agent.agents.reviewers import _category
from pr_agent.analyzers import AnalysisContext
from pr_agent.analyzers.ruff import run_ruff
from pr_agent.gitutil import materialize
from pr_agent.models import Finding, fix_type_for
from pr_agent.nodes.aggregate import assign_ids
from pr_agent.nodes.verify import deferrable
from pr_agent.policy.hard_rules import hard_rule_violations
from pr_agent.sandbox import LocalSandbox
from pr_agent.tracing import redact_text
from tests.unit._repo import make_pr

MARKER = "pwned.txt"


def test_pr_cannot_shadow_an_analyzer_module(tmp_path: Path) -> None:
    """#1: `python -m ruff` in the PR checkout must not import the PR's own ruff.py."""
    evil = f"open({str(tmp_path / MARKER)!r}, 'w').write('ran')\n"
    ctx = make_pr(tmp_path / "repo", {"a.py": "x = 1\n"}, {"ruff.py": evil, "a.py": "import os\n"})
    workdir = materialize(ctx.repo_path, ctx.head_sha)
    actx = AnalysisContext(
        workdir=workdir, base_dir=None, files=["a.py", "ruff.py"], sandbox=LocalSandbox()
    )
    findings = run_ruff(actx)
    assert not (tmp_path / MARKER).exists(), "PR code ran in place of the real ruff"
    assert any(f.rule_id == "ruff:F401" for f in findings)  # and the real ruff did run


def test_export_ignore_cannot_hide_files_from_scanners(tmp_path: Path) -> None:
    """#2: the head's .gitattributes must not change what gets materialized."""
    ctx = make_pr(
        tmp_path / "repo",
        {"a.py": "x = 1\n"},
        {".gitattributes": "leak.py export-ignore\n", "leak.py": "KEY = 'x'\n"},
    )
    assert (materialize(ctx.repo_path, ctx.head_sha) / "leak.py").exists()


@pytest.mark.parametrize("reported", ["format", "lint", "convention", "test"])
def test_llm_category_cannot_raise_fix_type(reported: str) -> None:
    """#3: an injected category from the bug reviewer must not become format/lint autonomy."""
    category = _category("bug", reported)  # type: ignore[arg-type]
    f = Finding(source="llm:bug", category=category, file="scripts/x.py", message="m")
    assert fix_type_for(f) == "bug"
    llm_format = Finding(source="llm:bug", category="format", file="x.py", message="m")
    assert fix_type_for(llm_format) == "bug"  # even if it slipped through


def _lint(line: int) -> Finding:
    return Finding(
        source="ruff", rule_id="ruff:E501", category="lint", file="a.py", line=line, message="long"
    )


def test_open_finding_never_inherits_a_fixed_findings_id() -> None:
    """#4: round 1 E501 at 10 (F1, fixed) and 50 (F2); round 2 only line 50 remains -> F2."""
    anchors1 = {("a.py", 10): "first long line", ("a.py", 50): "second long line"}
    r1, ids = assign_ids([_lint(10), _lint(50)], {}, anchors1)
    assert [f.id for f in r1] == ["F1", "F2"]
    r2, _ = assign_ids([_lint(49)], ids, {("a.py", 49): "second long line"}, landed={"F1"})
    assert [f.id for f in r2] == ["F2"]
    # Same key as a landed id (e.g. no anchor available): a fresh id, never the landed one.
    r3, _ = assign_ids([_lint(10)], {"a.py|ruff:E501||1": "F1"}, {}, landed={"F1"})
    assert r3[0].id != "F1"


@pytest.mark.parametrize(
    "secret",
    [
        'STRIPE_KEY = "sk_live_abcdefghij1234"',
        "postgres://admin:s3cr3tpw@db:5432/x",
        'password = "hunter2hunter2"',
        "Bearer eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4fw",
    ],
)
def test_redaction_covers_common_secret_shapes(secret: str) -> None:
    """#5."""
    assert "[REDACTED]" in redact_text(secret)


@pytest.mark.parametrize("comment", ["# nosec", "# noqa: S608", "# nosemgrep", "# gitleaks:allow"])
def test_a_fix_that_adds_a_suppression_is_forbidden(comment: str) -> None:
    """#6."""
    patch = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-q = f'{x}'\n+q = f'{x}'  " + comment + "\n"
    assert "no_suppression" in {name for name, _ in hard_rule_violations(patch)}


def test_only_fixable_ruff_lint_is_deferrable() -> None:
    """#8: e.g. F821 (not auto-fixable) or a semgrep rule must block, not wait a round."""
    ok = Finding(source="ruff", rule_id="ruff:F401", category="lint", file="a", message="m")
    f821 = ok.model_copy(update={"rule_id": "ruff:F821", "fixable": False})
    custom = ok.model_copy(update={"source": "semgrep", "rule_id": "custom:ERR-001"})
    assert deferrable(ok) and not deferrable(f821) and not deferrable(custom)


def test_hard_rules_cover_scanner_configs() -> None:
    """#11."""
    for path in [".gitattributes", ".gitleaks.toml", ".gitleaksignore", "conftest.py"]:
        patch = f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-a\n+b\n"
        assert hard_rule_violations(patch), path
