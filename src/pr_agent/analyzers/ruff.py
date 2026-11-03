"""Ruff lint + format checks, configured from the BASE sha, and deterministic ruff fixes.

Ruff normally discovers pyproject.toml/ruff.toml next to the files it checks, which in a PR
checkout is the head's config: a PR could switch rules off. So we always pass either
`--config <base config>` (ruff then ignores every discovered config) or `--isolated`.

The base config is copied into the head workdir (see base.stage_config) so that it is
reachable inside a container too. Ruff resolves paths in a `--config` file (per-file-ignores,
exclude) relative to the current directory, which is the repo root, so the copy behaves like
the original.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from pr_agent.agents.fixer import make_unified_diff
from pr_agent.analyzers.base import (
    AnalysisContext,
    python_files,
    relative_path,
    stage_config,
    tool_cmd,
    unstage_configs,
)
from pr_agent.gitutil import materialize
from pr_agent.models import Finding, PRContext
from pr_agent.sandbox import ExecResult, Sandbox

# Ruff's own precedence when several exist in one directory.
CONFIG_NAMES = (".ruff.toml", "ruff.toml", "pyproject.toml")
FORMAT_RULE = "ruff:format"
_HUNK = re.compile(r"^@@ -(\d+)")


def base_config(base_dir: Path | None) -> Path | None:
    """The base sha's ruff config file, or None (then ruff's defaults apply)."""
    if base_dir is None:
        return None
    return next((base_dir / n for n in CONFIG_NAMES if (base_dir / n).is_file()), None)


def config_args(workdir: Path, base_dir: Path | None) -> list[str]:
    """`--config <staged base config>` or `--isolated`. Never lets ruff read the head's config."""
    config = base_config(base_dir)
    if config is None:
        return ["--isolated"]
    # Keep the file name: ruff reads [tool.ruff] only from a file named pyproject.toml.
    return ["--config", stage_config(workdir, config, f"ruff/{config.name}")]


def _ruff(actx: AnalysisContext, subcommand: str, args: list[str]) -> ExecResult:
    """Run `ruff <subcommand> <args>` in the workdir with the base config, then unstage it."""
    try:
        cfg = config_args(actx.workdir, actx.base_dir)
        cmd = [*tool_cmd(actx.sandbox, "ruff"), subcommand, "--no-cache", *cfg, *args]
        return actx.sandbox.run(cmd, actx.workdir)
    finally:
        unstage_configs(actx.workdir)


def run_ruff(actx: AnalysisContext) -> list[Finding]:
    """Lint findings (one per violation) plus format findings (one per unformatted file)."""
    files = python_files(actx.files)
    if not files:
        return []
    return lint(actx, files) + format_check(actx, files)


def lint(actx: AnalysisContext, files: list[str]) -> list[Finding]:
    """`ruff check` JSON -> findings. Exit 1 just means violations were found."""
    res = _ruff(actx, "check", ["--output-format", "json", *files])
    if res.timed_out or res.returncode not in (0, 1):
        raise RuntimeError(f"ruff check exit {res.returncode}: {res.stderr.strip()[:300]}")
    return parse_ruff(res.stdout, actx.workdir)


def parse_ruff(output: str, workdir: Path) -> list[Finding]:
    """Turn ruff's JSON list into findings. `fixable` = ruff offers a *safe* fix (what --fix
    applies); syntax errors have no code and become ruff:invalid-syntax."""
    try:
        violations = json.loads(output or "[]")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ruff output is not JSON: {e}") from e
    return [
        Finding(
            source="ruff",
            rule_id=f"ruff:{v.get('code') or 'invalid-syntax'}",
            category="lint",
            severity="low",
            file=relative_path(v["filename"], workdir),
            line=int(v["location"]["row"]),
            end_line=int(v["end_location"]["row"]),
            message=v.get("message", ""),
            fixable=(v.get("fix") or {}).get("applicability") == "safe",
        )
        for v in violations
    ]


def format_check(actx: AnalysisContext, files: list[str]) -> list[Finding]:
    """`ruff format --check --diff`: exit 1 = some files would be reformatted."""
    res = _ruff(actx, "format", ["--check", "--diff", *files])
    if res.timed_out or res.returncode not in (0, 1):
        raise RuntimeError(f"ruff format exit {res.returncode}: {res.stderr.strip()[:300]}")
    return parse_format_diff(res.stdout, actx.workdir)


def parse_format_diff(diff: str, workdir: Path) -> list[Finding]:
    """One finding per file in the diff, placed on the first line the formatter would change."""
    findings: list[Finding] = []
    current: str | None = None
    line = 0
    for text in diff.splitlines():
        if text.startswith("--- "):
            current = relative_path(text[4:].strip(), workdir)
        elif current and (m := _HUNK.match(text)):
            line = int(m.group(1))
        elif current and line and text.startswith(("-", "+")) and not text.startswith("+++"):
            findings.append(
                Finding(
                    source="ruff",
                    rule_id=FORMAT_RULE,
                    category="format",
                    severity="low",
                    file=current,
                    line=line,
                    message="File is not formatted (run `ruff format`).",
                )
            )
            current, line = None, 0  # one finding per file: skip its remaining hunks
        elif current and line and text.startswith(" "):
            line += 1  # context line: the first change is further down
    return findings


def ruff_fix_patch(ctx: PRContext, finding: Finding, sandbox: Sandbox) -> str | None:
    """Deterministic fix for a ruff finding: a git-apply-able diff of that file, or None.

    Lint: `ruff check --fix-only --select <code>` (safe fixes for that rule only).
    Format: `ruff format`. Both use the base config, exactly like the analysis did.
    """
    if finding.source != "ruff" or not finding.rule_id:
        return None
    code = finding.rule_id.removeprefix("ruff:")
    workdir = materialize(ctx.repo_path, ctx.head_sha)
    base_dir = materialize(ctx.repo_path, ctx.base_sha)
    try:
        target = workdir / finding.file
        if not target.is_file():
            return None
        before = target.read_text()
        actx = AnalysisContext(workdir, base_dir, [finding.file], sandbox)
        if finding.rule_id == FORMAT_RULE:
            res = _ruff(actx, "format", [finding.file])
        else:
            res = _ruff(
                actx, "check", ["--fix-only", "--exit-zero", "--select", code, finding.file]
            )
        if not res.ok:
            return None
        after = target.read_text()
        return make_unified_diff(finding.file, before, after) if after != before else None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(base_dir, ignore_errors=True)
