"""Shared analyzer plumbing: the context every analyzer receives, and small path helpers.

Lives apart from `analyzers/__init__.py` so analyzer modules can import it without a cycle.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from pr_agent.sandbox import CONTAINER_WORKDIR, Sandbox

# Trusted configs (from the BASE sha) are copied here inside the head workdir, so tools in a
# container can read them through the single /work mount. Removed again after each run.
CONFIG_DIR = ".pr-agent-config"


class ToolUnavailable(RuntimeError):
    """The analyzer's binary is not installed. The analyze node reports it as skipped."""


@dataclass
class AnalysisContext:
    """What an analyzer gets. Repo-supplied configuration is read from base_dir, never workdir."""

    workdir: Path  # materialized head: the code under review
    base_dir: Path | None  # materialized base: trusted configs (None = use tool defaults)
    files: list[str]  # changed files that exist in head, relative to workdir
    sandbox: Sandbox


def python_files(files: list[str]) -> list[str]:
    """The Python sources among `files`."""
    return [f for f in files if f.endswith((".py", ".pyi"))]


def executable(sandbox: Sandbox, name: str) -> str:
    """A console script next to the sandbox's interpreter (the venv's bin/), else bare `name`."""
    python = Path(sandbox.python()[0])
    sibling = python.parent / name
    return str(sibling) if python.is_absolute() and sibling.exists() else name


def relative_path(filename: str, workdir: Path) -> str:
    """Normalise a tool-reported path (absolute host, /work/..., or "./x") to repo-relative."""
    path = Path(filename)
    if path.is_absolute():
        for root in (workdir.resolve(), Path(CONTAINER_WORKDIR)):
            if path.resolve().is_relative_to(root):
                return path.resolve().relative_to(root).as_posix()
    return path.as_posix().removeprefix("./")


def stage_config(workdir: Path, source: Path, name: str) -> str:
    """Copy a trusted config file or dir into workdir/CONFIG_DIR/name. Returns the relative path."""
    dest = workdir / CONFIG_DIR / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, dest, symlinks=True)
    else:
        shutil.copyfile(source, dest)
    return f"{CONFIG_DIR}/{name}"


def unstage_configs(workdir: Path) -> None:
    """Remove the staged configs so they never leak into later scans, tests or patches."""
    shutil.rmtree(workdir / CONFIG_DIR, ignore_errors=True)


def tool_cmd(sandbox: Sandbox, module: str) -> list[str]:
    """`python -P -m <tool>`: -P keeps the cwd (the PR checkout) off sys.path, so a PR that
    adds `ruff.py` or `bandit/__main__.py` cannot run code in place of the real tool."""
    return [*sandbox.python(), "-P", "-m", module]
