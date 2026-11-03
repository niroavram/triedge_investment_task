"""Deterministic validation of rule proposals. Every tool runs through the Sandbox protocol.

A machine-checkable rule must (1) fire on the head at the finding (+-2 lines) and (2) stay
silent there once the verified patch is applied. We also count (3) its hits on the base
checkout, so a reviewer sees how noisy enabling it would be. Any failed check -> invalid.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import tempfile
import textwrap
from collections.abc import Callable
from pathlib import Path

from pr_agent.analyzers.base import (
    CONFIG_DIR,
    executable,
    relative_path,
    unstage_configs,
)
from pr_agent.learning.existing_rules import RuleHit, bandit_hits, ruff_hits
from pr_agent.models import Finding, RuleProposal
from pr_agent.sandbox import Sandbox

NOISY = 5  # more base hits than this -> "would also flag N existing places"
CONV_ID = re.compile(r"CONV-\d{3}")
CONVENTION_NOTE = "convention (not machine-checked)"
PARTIAL_NOTE = "partially verified (no fixed code)"
Scan = Callable[[Path, str], list[RuleHit]]  # (workdir, target) -> hits


def invalid(proposal: RuleProposal, reason: str) -> RuleProposal:
    return proposal.model_copy(update={"valid": False, "note": f"invalid: {reason}"})


def flag_noise(proposal: RuleProposal) -> RuleProposal:
    """Keep noisy proposals, but say how many existing places they would flag."""
    if proposal.base_hits <= NOISY:
        return proposal
    note = f"{proposal.note} — would also flag {proposal.base_hits} existing places".lstrip(" —")
    return proposal.model_copy(update={"note": note})


def bare_id(rule_id: str) -> str:
    """`custom:SEC-002` -> `SEC-002`; `ruff:S608` -> `S608`."""
    return rule_id.split(":", 1)[-1]


# --- semgrep -------------------------------------------------------------------------------


def semgrep_test(sandbox: Sandbox, rule_yaml: str, test_code: str, name: str) -> tuple[bool, str]:
    """`semgrep --test` on `<name>.yaml` + `<name>.py` (the pairing semgrep expects)."""
    tmp = Path(tempfile.mkdtemp(prefix="pr-agent-rule-"))
    try:
        (tmp / f"{name}.yaml").write_text(rule_yaml)
        (tmp / f"{name}.py").write_text(test_code)
        cmd = [executable(sandbox, "semgrep"), "--test", "--metrics=off", "."]
        res = sandbox.run(cmd, tmp)
        return res.ok, (res.stdout + res.stderr).strip()[-300:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def semgrep_hits(sandbox: Sandbox, workdir: Path, rule_yaml: str, target: str) -> list[RuleHit]:
    """Results of one rule on target (a file or "."), with repo-relative paths."""
    staged = workdir / CONFIG_DIR / "learn" / "rule.yaml"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(rule_yaml)
    try:
        cmd = [
            executable(sandbox, "semgrep"), "scan", "--metrics=off", "--disable-version-check",
            "--json", "--config", f"{CONFIG_DIR}/learn/rule.yaml", target,
        ]  # fmt: skip
        res = sandbox.run(cmd, workdir)
    finally:
        unstage_configs(workdir)
    if res.timed_out or res.returncode not in (0, 1):
        raise RuntimeError(f"semgrep exit {res.returncode}: {res.stderr.strip()[-300:]}")
    return [
        RuleHit(
            "semgrep",
            r["check_id"].rsplit(".", 1)[-1],
            relative_path(r["path"], workdir),
            int(r["start"]["line"]),
            int(r["end"]["line"]),
        )
        for r in json.loads(res.stdout or "{}").get("results", [])
    ]


def _fires(hits: list[RuleHit], finding: Finding) -> bool:
    return any(h.file == finding.file and h.near(finding.line) for h in hits)


def _location_checks(
    proposal: RuleProposal,
    finding: Finding,
    scan: Scan,
    head_dir: Path,
    fixed_dir: Path | None,
    base_dir: Path,
) -> RuleProposal:
    """Fire on head, silent on head + patch (when there is one), and count base hits."""
    if not _fires(scan(head_dir, finding.file), finding):
        return invalid(proposal, f"does not fire at {finding.file}:{finding.line}")
    if fixed_dir is not None and _fires(scan(fixed_dir, finding.file), finding):
        return invalid(proposal, "still fires on the fixed code")
    hits = len(scan(base_dir, "."))
    return flag_noise(proposal.model_copy(update={"valid": True, "base_hits": hits}))


def verify_semgrep(
    proposal: RuleProposal,
    finding: Finding,
    sandbox: Sandbox,
    head_dir: Path,
    fixed_dir: Path,
    base_dir: Path,
) -> RuleProposal:
    """`semgrep --test` passes, fires on head, silent on the fix, base hits counted."""
    ok, log = semgrep_test(
        sandbox, proposal.content, proposal.test_content or "", bare_id(proposal.rule_id)
    )
    if not ok:
        return invalid(proposal, f"semgrep --test failed: {log}")

    def scan(workdir: Path, target: str) -> list[RuleHit]:
        return semgrep_hits(sandbox, workdir, proposal.content, target)

    return _location_checks(proposal, finding, scan, head_dir, fixed_dir, base_dir)


# --- existing ruff / bandit rule --------------------------------------------------------------


def verify_existing(
    proposal: RuleProposal,
    finding: Finding,
    sandbox: Sandbox,
    head_dir: Path,
    fixed_dir: Path | None,
    base_dir: Path,
) -> RuleProposal:
    """Same fire / silent / base_hits checks with `ruff --select <code>` or `bandit -t <id>`.

    fixed_dir None (critical/high paths, no fix attempted) skips the silent check.
    """
    tool, _, code = proposal.rule_id.partition(":")

    def scan(workdir: Path, target: str) -> list[RuleHit]:
        if tool == "ruff":
            return ruff_hits(sandbox, workdir, code, target)
        return [h for h in bandit_hits(sandbox, workdir, target, code) if h.code == code]

    if tool not in ("ruff", "bandit"):
        return invalid(proposal, f"unknown tool {tool!r}")
    return _location_checks(proposal, finding, scan, head_dir, fixed_dir, base_dir)


# --- convention ------------------------------------------------------------------------------


def _parses(code: str) -> bool:
    try:
        ast.parse(textwrap.dedent(code).strip())
    except SyntaxError:
        return False
    return True


def verify_convention(
    proposal: RuleProposal, bad: str, good: str, existing_ids: set[str]
) -> RuleProposal:
    """Well-formed unique CONV-ID, both examples parse, and they differ. Not machine-checked."""
    if not CONV_ID.fullmatch(proposal.rule_id):
        return invalid(proposal, f"malformed id {proposal.rule_id!r}")
    if proposal.rule_id in existing_ids:
        return invalid(proposal, f"{proposal.rule_id} already exists")
    if not (_parses(bad) and _parses(good)):
        return invalid(proposal, "an example does not parse")
    if bad.strip() == good.strip():
        return invalid(proposal, "bad and good examples are identical")
    return proposal.model_copy(update={"valid": True, "note": CONVENTION_NOTE})
