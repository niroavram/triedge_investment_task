"""Bandit (Python security linter) runner: JSON output -> Finding objects."""

from __future__ import annotations

import json
from pathlib import Path

from pr_agent.analyzers.base import AnalysisContext, python_files, relative_path, tool_cmd
from pr_agent.models import Finding, Severity

_SEVERITY: dict[str, Severity] = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}
_CONFIDENCE = {"LOW": 0.5, "MEDIUM": 0.8, "HIGH": 1.0}


def run_bandit(actx: AnalysisContext) -> list[Finding]:
    """Run bandit on the changed .py files. Raises RuntimeError if bandit itself fails."""
    files = python_files(actx.files)
    if not files:
        return []
    # B101 (assert used) is skipped: asserts are the pytest idiom, flagging them is noise.
    cmd = [
        *tool_cmd(actx.sandbox, "bandit"),
        "-q",
        "-f",
        "json",
        "-s",
        "B101",
        "--ignore-nosec",
        *files,
    ]
    res = actx.sandbox.run(cmd, actx.workdir)
    if res.timed_out or res.returncode not in (0, 1):  # 1 = issues found, not an error
        raise RuntimeError(f"bandit exit {res.returncode}: {res.stderr.strip()[:300]}")
    return parse_bandit(res.stdout, actx.workdir)


def parse_bandit(output: str, workdir: Path) -> list[Finding]:
    """Turn bandit's JSON report into findings with repo-relative paths."""
    try:
        results = json.loads(output).get("results", [])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"bandit output is not JSON: {e}") from e
    return [
        Finding(
            source="bandit",
            rule_id=f"bandit:{r['test_id']}",
            category="security",
            severity=_SEVERITY.get(r.get("issue_severity", ""), "medium"),
            file=relative_path(r["filename"], workdir),
            line=int(r.get("line_number", 1)),
            message=r.get("issue_text", ""),
            confidence=_CONFIDENCE.get(r.get("issue_confidence", ""), 0.5),
        )
        for r in results
    ]
