import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from pr_agent.analyzers import ANALYZERS, AnalysisContext
from pr_agent.analyzers.base import CONFIG_DIR
from pr_agent.analyzers.ruff import parse_format_diff, parse_ruff, ruff_fix_patch, run_ruff
from pr_agent.gitutil import apply_patch, materialize
from pr_agent.models import Finding, PRContext
from pr_agent.sandbox import LocalSandbox
from tests.fakes import FakeSandbox
from tests.unit._repo import make_pr

BASE_PYPROJECT = (Path(__file__).parents[2] / "demo/base/pyproject.toml").read_text()
# The PR tries to switch every rule but E501 off. Must have no effect.
HEAD_PYPROJECT = '[tool.ruff.lint]\nselect = ["E501"]\n'
APP = "import os\n\n\ndef total(xs: list[int]) -> int:\n    return sum(xs)\n"
UGLY = "def f( a ):\n    return a\n"


@pytest.fixture
def pr(tmp_path: Path) -> PRContext:
    return make_pr(
        tmp_path / "repo",
        base={"pyproject.toml": BASE_PYPROJECT, "src/__init__.py": ""},
        head={"pyproject.toml": HEAD_PYPROJECT, "src/app.py": APP, "src/ugly.py": UGLY},
    )


@pytest.fixture
def actx(pr: PRContext) -> Iterator[AnalysisContext]:
    workdir = materialize(pr.repo_path, pr.head_sha)
    base_dir = materialize(pr.repo_path, pr.base_sha)
    yield AnalysisContext(workdir, base_dir, ["src/app.py", "src/ugly.py"], LocalSandbox())
    shutil.rmtree(workdir)
    shutil.rmtree(base_dir)


def test_registered() -> None:
    assert ANALYZERS["ruff"] is run_ruff


def test_base_config_wins_over_head(actx: AnalysisContext) -> None:
    findings = run_ruff(actx)
    [f401] = [f for f in findings if f.rule_id == "ruff:F401"]
    assert (f401.file, f401.line, f401.category, f401.severity) == ("src/app.py", 1, "lint", "low")
    assert f401.fixable and f401.source == "ruff"
    assert not (actx.workdir / CONFIG_DIR).exists()  # staged config cleaned up


def test_format_finding_one_per_file(actx: AnalysisContext) -> None:
    fmt = [f for f in run_ruff(actx) if f.category == "format"]
    assert [(f.rule_id, f.file, f.line) for f in fmt] == [("ruff:format", "src/ugly.py", 1)]


def test_no_base_config_runs_isolated(tmp_path: Path) -> None:
    sandbox = FakeSandbox()
    run_ruff(AnalysisContext(tmp_path, None, ["a.py", "notes.md"], sandbox))
    check, fmt = sandbox.commands
    assert check == ["python", "-P", "-m", "ruff", "check", "--no-cache", "--isolated"] + [
        "--output-format",
        "json",
        "a.py",
    ]
    assert fmt[4:] == ["format", "--no-cache", "--isolated", "--check", "--diff", "a.py"]


def test_fix_patch_removes_import(pr: PRContext) -> None:
    finding = Finding(source="ruff", rule_id="ruff:F401", category="lint", file="src/app.py",
                      message="unused")  # fmt: skip
    patch = ruff_fix_patch(pr, finding, LocalSandbox())
    assert patch and patch.startswith("--- a/src/app.py\n+++ b/src/app.py\n")
    workdir = materialize(pr.repo_path, pr.head_sha)
    try:
        ok, err = apply_patch(workdir, patch)
        assert ok, err
        fixed = (workdir / "src/app.py").read_text()
        assert "import os" not in fixed and "def total" in fixed
        assert (workdir / "src/ugly.py").read_text() == UGLY  # that file only
    finally:
        shutil.rmtree(workdir)


def test_fix_patch_format_and_noop(pr: PRContext) -> None:
    fmt = Finding(source="ruff", rule_id="ruff:format", category="format", file="src/ugly.py",
                  message="fmt")  # fmt: skip
    assert "+def f(a):" in (ruff_fix_patch(pr, fmt, LocalSandbox()) or "")
    nothing = fmt.model_copy(update={"rule_id": "ruff:E711", "category": "lint"})
    assert ruff_fix_patch(pr, nothing, LocalSandbox()) is None


def test_parse_container_paths_and_syntax_errors(tmp_path: Path) -> None:
    out = json.dumps(
        [
            {
                "code": None,
                "filename": "/work/src/a.py",
                "location": {"row": 3, "column": 1},
                "end_location": {"row": 3, "column": 4},
                "message": "SyntaxError",
                "fix": None,
            }
        ]
    )
    [f] = parse_ruff(out, tmp_path)
    assert (f.rule_id, f.file, f.line, f.fixable) == ("ruff:invalid-syntax", "src/a.py", 3, False)
    diff = "--- a.py\n+++ a.py\n@@ -4,4 +4,4 @@\n x\n y\n-z=1\n+z = 1\n"
    assert [(f.file, f.line) for f in parse_format_diff(diff, tmp_path)] == [("a.py", 6)]
