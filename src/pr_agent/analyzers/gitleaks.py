"""Gitleaks secret scan over the PR's changed files only.

The changed files are copied into a scratch dir so gitleaks sees nothing else. The head's
.gitleaks.toml / .gitleaksignore are never copied (a PR must not allowlist its own secret);
the base's .gitleaks.toml is used when present. `--redact` keeps the secret out of the report,
and messages use the rule Description only: secret values never reach findings or logs.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from pr_agent.analyzers.base import AnalysisContext, ToolUnavailable, relative_path
from pr_agent.models import Finding

CONFIG_FILES = {".gitleaks.toml", ".gitleaksignore"}
BASE_CONFIG = ".gitleaks.toml"
# Inside the scratch dir, under a name gitleaks does not auto-load.
STAGED_CONFIG = ".pr-agent-gitleaks.toml"
NOT_INSTALLED = 127  # shell/exec convention, also what LocalSandbox returns for a missing binary
LEAKS_FOUND = 1


def stage_files(actx: AnalysisContext, dest: Path) -> list[str]:
    """Copy the changed files (minus gitleaks config) into dest. Returns gitleaks' extra args."""
    for rel in actx.files:
        if Path(rel).name in CONFIG_FILES or not (actx.workdir / rel).is_file():
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(actx.workdir / rel, dest / rel)
    config = actx.base_dir / BASE_CONFIG if actx.base_dir else None
    if config and config.is_file():
        shutil.copyfile(config, dest / STAGED_CONFIG)
        return ["--config", STAGED_CONFIG]
    return []


def run_gitleaks(actx: AnalysisContext) -> list[Finding]:
    """Scan the changed files. Raises ToolUnavailable if gitleaks is not installed."""
    if not actx.files:
        return []
    scratch = Path(tempfile.mkdtemp(prefix="pr-agent-gitleaks-"))
    try:
        extra = stage_files(actx, scratch)
        cmd = [
            "gitleaks", "dir", "--no-banner", "--redact", "--ignore-gitleaks-allow",
            "--report-format", "json", "--report-path", "-",
            *extra, ".",
        ]  # fmt: skip
        res = actx.sandbox.run(cmd, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if res.returncode == NOT_INSTALLED:
        raise ToolUnavailable("not installed")
    if res.timed_out or res.returncode not in (0, LEAKS_FOUND):
        # stderr only: gitleaks logs rule names and paths there, the report goes to stdout
        raise RuntimeError(f"gitleaks exit {res.returncode}: {res.stderr.strip()[-300:]}")
    return parse_gitleaks(res.stdout, scratch)


def parse_gitleaks(output: str, scan_dir: Path) -> list[Finding]:
    """Gitleaks' JSON array -> findings. Only metadata is read; Secret/Match are never used."""
    try:
        leaks = json.loads(output or "[]") or []
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gitleaks output is not JSON: {e}") from e
    return [
        Finding(
            source="gitleaks",
            rule_id=f"gitleaks:{leak['RuleID']}",
            category="security",
            severity="high",
            file=relative_path(leak["File"], scan_dir),
            line=int(leak.get("StartLine", 1)),
            message=f"Possible secret: {leak.get('Description') or leak['RuleID']}",
        )
        for leak in leaks
    ]
