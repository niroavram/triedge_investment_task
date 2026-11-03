"""Semgrep with vendored security rules plus the BASE sha's `.semgrep/` rules. Fully offline.

Never `--config auto` or registry refs: those need the network and would let the rule set
change under us. The head's `.semgrep/` is ignored, so a PR cannot weaken its own review.
Both rule sets are copied into the workdir (base.stage_config) so a container can read them.
"""

from __future__ import annotations

import json
from pathlib import Path

from pr_agent.analyzers.base import (
    AnalysisContext,
    executable,
    relative_path,
    stage_config,
    unstage_configs,
)
from pr_agent.models import Category, Finding, Severity

VENDORED_RULES = Path(__file__).with_name("semgrep_rules")
BASE_RULES_DIR = ".semgrep"
# Staged directory names. Semgrep prefixes each rule id with its config path, dot-separated
# (".pr-agent-config/semgrep_vendored/security.yml" -> "pr-agent-config.semgrep_vendored.<id>"),
# which is how we tell vendored rules from the repo's own.
_VENDORED = "semgrep_vendored"
_CUSTOM = "semgrep_custom"
_SEVERITY: dict[str, Severity] = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}


def config_args(actx: AnalysisContext) -> list[str]:
    """`--config` flags: vendored rules always, the base `.semgrep/` dir when it exists."""
    args = ["--config", stage_config(actx.workdir, VENDORED_RULES, _VENDORED)]
    custom = actx.base_dir / BASE_RULES_DIR if actx.base_dir else None
    if custom and custom.is_dir():
        args += ["--config", stage_config(actx.workdir, custom, _CUSTOM)]
    return args


def run_semgrep(actx: AnalysisContext) -> list[Finding]:
    """Scan the changed files. Raises RuntimeError if semgrep fails (exit 1 = findings)."""
    if not actx.files:
        return []
    try:
        cmd = [
            executable(actx.sandbox, "semgrep"),
            "scan",
            "--metrics=off",
            "--disable-nosem",  # a PR's `# nosemgrep` must not hide its own findings
            "--disable-version-check",
            "--json",
            *config_args(actx),
            *actx.files,
        ]
        res = actx.sandbox.run(cmd, actx.workdir)
    finally:
        unstage_configs(actx.workdir)
    if res.timed_out or res.returncode not in (0, 1):
        raise RuntimeError(f"semgrep exit {res.returncode}: {res.stderr.strip()[-300:]}")
    return parse_semgrep(res.stdout, actx.workdir)


def rule_id_and_category(check_id: str) -> tuple[str, Category]:
    """Map semgrep's path-prefixed check_id to our rule id and category."""
    rule = check_id.rsplit(".", 1)[-1]
    if f"{_VENDORED}." in check_id:
        return f"semgrep:{rule}", "security"
    return f"custom:{rule}", "security" if rule.startswith("SEC-") else "lint"


def parse_semgrep(output: str, workdir: Path) -> list[Finding]:
    """Semgrep's JSON report -> findings."""
    try:
        results = json.loads(output).get("results", [])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"semgrep output is not JSON: {e}") from e
    findings = []
    for r in results:
        rule_id, category = rule_id_and_category(r["check_id"])
        extra = r.get("extra", {})
        findings.append(
            Finding(
                source="semgrep",
                rule_id=rule_id,
                category=category,
                severity=_SEVERITY.get(extra.get("severity", ""), "medium"),
                file=relative_path(r["path"], workdir),
                line=int(r["start"]["line"]),
                end_line=int(r["end"]["line"]),
                message=extra.get("message", ""),
            )
        )
    return findings
