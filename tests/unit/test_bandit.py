import json
import shutil
from pathlib import Path

import pytest

from pr_agent.analyzers import ANALYZERS, AnalysisContext
from pr_agent.analyzers.bandit import run_bandit
from pr_agent.gitutil import materialize
from pr_agent.sandbox import ExecResult, LocalSandbox, Sandbox
from tests.fakes import FakeSandbox

CASE = "case/01-sql-injection"


def _actx(sandbox: Sandbox, workdir: Path, files: list[str]) -> AnalysisContext:
    return AnalysisContext(workdir=workdir, base_dir=None, files=files, sandbox=sandbox)


def test_registry_has_bandit() -> None:
    assert ANALYZERS["bandit"] is run_bandit


def test_real_bandit_finds_b608(demo_repo: Path) -> None:
    workdir = materialize(demo_repo, CASE)
    try:
        findings = run_bandit(_actx(LocalSandbox(), workdir, ["src/store/orders.py"]))
    finally:
        shutil.rmtree(workdir)
    b608 = [f for f in findings if f.rule_id == "bandit:B608"]
    assert len(b608) == 1
    f = b608[0]
    assert (f.file, f.line, f.source, f.category) == (
        "src/store/orders.py",
        20,
        "bandit",
        "security",
    )


def _report(filename: str) -> str:
    return json.dumps(
        {
            "results": [
                {
                    "filename": filename,
                    "test_id": "B105",
                    "issue_severity": "LOW",
                    "issue_confidence": "MEDIUM",
                    "line_number": 7,
                    "issue_text": "Possible hardcoded password",
                }
            ]
        }
    )


def test_parses_canned_json(tmp_path: Path) -> None:
    sandbox = FakeSandbox({"bandit": ExecResult(1, _report("./src/a.py"), "")})
    [f] = run_bandit(_actx(sandbox, tmp_path, ["src/a.py", "README.md"]))
    assert f.rule_id == "bandit:B105" and f.file == "src/a.py" and f.line == 7
    assert f.severity == "low" and f.confidence == 0.8
    assert sandbox.commands == [
        [
            "python",
            "-P",
            "-m",
            "bandit",
            "-q",
            "-f",
            "json",
            "-s",
            "B101",
            "--ignore-nosec",
            "src/a.py",
        ]
    ]  # the sandbox's interpreter, .py only


def test_absolute_paths_made_relative(tmp_path: Path) -> None:
    sandbox = FakeSandbox({"bandit": ExecResult(1, _report(str(tmp_path / "src/a.py")), "")})
    assert run_bandit(_actx(sandbox, tmp_path, ["src/a.py"]))[0].file == "src/a.py"


def test_no_python_files_skips_tool(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    assert run_bandit(_actx(sandbox, tmp_path, ["a.md"])) == [] and sandbox.commands == []


def test_tool_failure_raises(tmp_path: Path) -> None:
    sandbox = FakeSandbox({"bandit": ExecResult(2, "", "boom")})
    with pytest.raises(RuntimeError, match="boom"):
        run_bandit(_actx(sandbox, tmp_path, ["a.py"]))
