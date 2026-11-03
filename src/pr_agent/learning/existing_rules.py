"""Ladder step (a): an existing ruff/bandit rule that would have caught the finding.

Deterministic, no LLM. "Enabled" always means enabled by the BASE checkout's config, so a
PR cannot make its own rules look enabled (or disabled).
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pr_agent.analyzers.base import relative_path, tool_cmd
from pr_agent.analyzers.ruff import base_config
from pr_agent.models import Finding
from pr_agent.sandbox import Sandbox

SLACK = 2  # a rule "fires at the finding" within +-2 lines
RUFF_DEFAULT_SELECT = ["E4", "E7", "E9", "F"]  # ruff's own default when no config selects
# Bandit runs with all tests except B101 (see analyzers/bandit.py), so only B101 is "off".
BANDIT_SKIPPED = {"B101"}
# Pure style families: enabling them would not prevent a bug or a vulnerability.
STYLE_FAMILIES = (
    "D", "ANN", "COM", "Q", "ERA", "T20", "FBT", "EM", "TRY003", "PLR2004", "INP", "CPY",
)  # fmt: skip
_SECURITY = re.compile(r"^(S\d|B\d)")  # ruff flake8-bandit S*, bandit B*
_BUGBEAR_LIKE = re.compile(r"^(B\d|BLE|PLE|PLW|RUF|ASYNC|DTZ|PIE|RET|SIM|PERF)")


@dataclass(frozen=True)
class RuleHit:
    tool: str  # "ruff" | "bandit"
    code: str  # "S608", "B608"
    file: str
    line: int
    end_line: int

    @property
    def rule_id(self) -> str:
        return f"{self.tool}:{self.code}"

    def near(self, line: int) -> bool:
        return self.line - SLACK <= line <= self.end_line + SLACK


def selector_matches(code: str, selector: str) -> bool:
    """Ruff selector semantics: a prefix of the code, but `D` selects D100, not DTZ005."""
    if selector == "ALL":
        return True
    if not code.startswith(selector):
        return False
    if selector == "PL" or len(code) == len(selector) or selector[-1].isdigit():
        return True
    return code[len(selector)].isdigit()


def ruff_selection(base_dir: Path | None) -> tuple[list[str], list[str]]:
    """(select + extend-select, ignore + extend-ignore) from the base ruff config."""
    config = base_config(base_dir)
    if config is None:
        return list(RUFF_DEFAULT_SELECT), []
    data = tomllib.loads(config.read_text())
    if config.name == "pyproject.toml":
        data = data.get("tool", {}).get("ruff", {})
    tables = [data, data.get("lint", {})]  # top-level keys are ruff's legacy spelling
    select = next((t["select"] for t in reversed(tables) if "select" in t), RUFF_DEFAULT_SELECT)
    extra = [s for t in tables for s in t.get("extend-select", [])]
    ignore = [s for t in tables for key in ("ignore", "extend-ignore") for s in t.get(key, [])]
    return [*select, *extra], ignore


def is_enabled(rule_id: str | None, selection: tuple[list[str], list[str]]) -> bool:
    """True when the repo's OWN base config enforces this rule, so its CI already prevents it.

    Only ruff rules selected in the base config and the base `.semgrep/` rules (`custom:`)
    count. Bandit and our vendored semgrep rules run inside this agent, not in the repo's CI,
    so a finding they catch is still worth turning into a repo rule. LLM findings,
    conventions and test failures are never "enabled".
    """
    tool, _, code = (rule_id or "").partition(":")
    if tool == "ruff":
        select, ignore = selection
        return any(selector_matches(code, s) for s in select) and not any(
            selector_matches(code, s) for s in ignore
        )
    return tool == "custom"


def is_style(code: str) -> bool:
    return any(selector_matches(code, family) for family in STYLE_FAMILIES)


def priority(hit: RuleHit) -> tuple[int, int]:
    """Ruff before bandit (the repo enforces rules through its ruff config, so a ruff rule is
    the actionable proposal); within a tool: security, then bugbear-like, then the rest."""
    kind = 0 if _SECURITY.match(hit.code) else 1 if _BUGBEAR_LIKE.match(hit.code) else 2
    return (0 if hit.tool == "ruff" else 1, kind)


def _ruff_cmd(sandbox: Sandbox, select: str, target: str) -> list[str]:
    return [
        *tool_cmd(sandbox, "ruff"), "check", "--no-cache", "--isolated",
        "--select", select, "--output-format", "json", "--exit-zero", target,
    ]  # fmt: skip


def _bandit_cmd(sandbox: Sandbox, target: str, test: str | None = None) -> list[str]:
    cmd = [*tool_cmd(sandbox, "bandit"), "-q", "-f", "json"]
    if test:
        cmd += ["-t", test]
    return [*cmd, "-r", target] if target == "." else [*cmd, target]


def ruff_hits(sandbox: Sandbox, workdir: Path, select: str, target: str) -> list[RuleHit]:
    """`ruff check --isolated --select <select>` on target (a file or ".")."""
    res = sandbox.run(_ruff_cmd(sandbox, select, target), workdir)
    if res.timed_out or res.returncode != 0:
        raise RuntimeError(f"ruff exit {res.returncode}: {res.stderr.strip()[:300]}")
    return [
        RuleHit(
            "ruff",
            v["code"],
            relative_path(v["filename"], workdir),
            int(v["location"]["row"]),
            int(v["end_location"]["row"]),
        )
        for v in json.loads(res.stdout or "[]")
        if v.get("code")
    ]


def bandit_hits(
    sandbox: Sandbox, workdir: Path, target: str, test: str | None = None
) -> list[RuleHit]:
    """Full bandit (no skips), or just test `-t <id>`, on target (a file or ".")."""
    res = sandbox.run(_bandit_cmd(sandbox, target, test), workdir)
    if res.timed_out or res.returncode not in (0, 1):
        raise RuntimeError(f"bandit exit {res.returncode}: {res.stderr.strip()[:300]}")
    results = json.loads(res.stdout or "{}").get("results", [])
    return [
        RuleHit(
            "bandit",
            r["test_id"],
            relative_path(r["filename"], workdir),
            int(r["line_number"]),
            int((r.get("line_range") or [r["line_number"]])[-1]),
        )
        for r in results
    ]


def candidates(
    sandbox: Sandbox, head_dir: Path, finding: Finding, selection: tuple[list[str], list[str]]
) -> list[RuleHit]:
    """Not-yet-enabled, non-style rules that fire within +-2 lines of the finding, best first."""
    hits = ruff_hits(sandbox, head_dir, "ALL", finding.file)
    hits += bandit_hits(sandbox, head_dir, finding.file)
    keep = [
        h
        for h in hits
        if h.near(finding.line) and not is_style(h.code) and not is_enabled(h.rule_id, selection)
    ]
    unique = {h.rule_id: h for h in reversed(keep)}  # one per rule, first occurrence wins
    return sorted(unique.values(), key=lambda h: (priority(h), h.line, h.code))


def enable_snippet(rule_id: str) -> str:
    """The config change a human would make to turn the rule on."""
    tool, _, code = rule_id.partition(":")
    if tool == "bandit":
        return f"# bandit: stop skipping {code} (drop it from `-s` / `skips`)\nskips = []\n"
    return f'# pyproject.toml\n[tool.ruff.lint]\nextend-select = ["{code}"]\n'
