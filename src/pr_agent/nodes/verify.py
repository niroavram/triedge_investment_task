"""Fix verification: apply the patch to a clean head checkout and prove it helped, not hurt."""

from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

from langchain_core.runnables import RunnableConfig

from pr_agent.analyzers import AnalysisContext, run_all
from pr_agent.deps import Deps, get_deps
from pr_agent.gitutil import apply_patch, materialize
from pr_agent.models import Finding, FixProposal, PRContext, Verification
from pr_agent.policy.engine import patch_paths
from pr_agent.sandbox import Sandbox

GONE_SLACK = 2  # the fix may move the flagged statement by a line or two
LOG_LIMIT = 2000
NO_TESTS_COLLECTED = 5  # pytest exit code: "no tests ran"


def scan(
    sandbox: Sandbox, workdir: Path, base_dir: Path | None, files: list[str]
) -> tuple[list[Finding], list[str]]:
    """Run all analyzers on files that exist in workdir, configured from base_dir.

    Returns (findings, error messages). A tool that is not installed is not an error: it
    would equally be missing before and after the patch.
    """
    present = [f for f in files if (workdir / f).is_file()]
    findings, status = run_all(AnalysisContext(workdir, base_dir, present, sandbox))
    errors = [f"{name}: {s}" for name, s in status.items() if s.startswith("error")]
    return findings, errors


def finding_gone(original: Finding, after: list[Finding]) -> bool:
    """A tool finding is gone when no same-rule finding sits near its original line."""
    if original.source.startswith("llm:") or original.rule_id is None:
        return True  # nothing deterministic to re-check; the critic judged it
    return not any(
        f.rule_id == original.rule_id
        and f.file == original.file
        and abs(f.line - original.line) <= GONE_SLACK
        for f in after
    )


# New lint/format findings don't fail a fix: the convergence loop fixes them next round.
DEFERRABLE = {"lint", "format"}


def deferrable(f: Finding) -> bool:
    """Lint/format a deterministic tool can fix next round (not e.g. F821 or a ruff S-rule)."""
    return f.category in DEFERRABLE and f.source == "ruff" and f.fixable


def new_findings(before: list[Finding], after: list[Finding]) -> list[Finding]:
    """Findings whose (rule_id, file) count grew after the patch. Lines shift, so ignore them."""
    remaining = Counter((f.rule_id, f.file) for f in before)
    grown = []
    for f in after:
        key = (f.rule_id, f.file)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            grown.append(f)
    return grown


def run_tests(sandbox: Sandbox, workdir: Path, timeout: int) -> tuple[bool, str]:
    """pytest in the patched checkout; "no tests ran" counts as a pass. Returns (ok, log tail)."""
    cmd = [*sandbox.python(), "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]
    res = sandbox.run(cmd, workdir, timeout=timeout)
    ok = not res.timed_out and res.returncode in (0, NO_TESTS_COLLECTED)
    tail = (res.stdout + res.stderr).strip().splitlines()[-5:]
    return ok, "timed out" if res.timed_out else " | ".join(tail)


def check_patch(
    deps: Deps,
    finding: Finding,
    ctx: PRContext,
    patch: str,
    workdir: Path,
    base_dir: Path | None = None,
    can_defer: bool = False,
) -> tuple[dict[str, bool], list[str]]:
    """Run the checks in order inside workdir (a clean head checkout). Returns (checks, log).

    base_dir is the materialized base sha, the source of analyzer configs.
    """
    files = patch_paths(patch)
    ok, err = apply_patch(workdir, patch, check_only=True)
    if not ok:
        return {"applies": False}, [f"git apply --check failed: {err.strip()}"]
    before, errors_before = scan(deps.sandbox, workdir, base_dir, files)
    ok, err = apply_patch(workdir, patch)
    if not ok:
        return {"applies": False}, [f"git apply failed: {err.strip()}"]
    after, errors_after = scan(deps.sandbox, workdir, base_dir, files)
    errors = errors_before + errors_after
    grown = new_findings(before, after)
    blocking = [g for g in grown if not (can_defer and deferrable(g))]
    log = [f"analyzer error: {e}" for e in errors]
    log += [f"new finding: {g.rule_id} in {g.file}" for g in blocking]
    log += [
        f"introduced {g.rule_id} in {g.file} (deferred to next round)"
        for g in grown
        if g not in blocking
    ]
    checks = {
        "applies": True,
        # fail closed: if an analyzer could not run, we cannot claim what it would have checked
        "finding_gone": not errors and finding_gone(finding, after),
        "no_new_findings": not errors and not blocking,
    }
    if deps.sandbox.isolated or ctx.mode == "local":
        checks["tests_pass"], test_log = run_tests(
            deps.sandbox, workdir, deps.settings.test_timeout
        )
        log.append(f"pytest: {test_log}")
    else:
        log.append("tests_pass skipped: sandbox is not isolated and PR code is untrusted")
    return checks, log


def verify_fix(state: dict, config: RunnableConfig) -> dict:
    """Fix-pipeline node: verify state["proposal"] and emit a Verification plus the proposal."""
    proposal: FixProposal | None = state.get("proposal")
    if proposal is None:
        return {}
    deps = get_deps(config)
    ctx: PRContext = state["ctx"]
    workdir = materialize(ctx.repo_path, ctx.head_sha)
    base_dir = materialize(ctx.repo_path, ctx.base_sha)
    try:
        checks, log = check_patch(
            deps,
            state["finding"],
            ctx,
            proposal.patch,
            workdir,
            base_dir,
            can_defer=state.get("can_defer", False),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(base_dir, ignore_errors=True)
    verification = Verification(
        finding_id=proposal.finding_id,
        passed=all(checks.values()),
        checks=checks,
        log="\n".join(log)[:LOG_LIMIT],
    )
    return {"verifications": [verification], "fixes": [proposal]}
