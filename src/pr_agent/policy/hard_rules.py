"""Hard rules: things the agent may never do, whatever `.pr-agent.yml` says.

Not configurable on purpose: a PR (or a prompt-injected LLM) must not be able to loosen them.
Any violation makes the decision FORBIDDEN with decided_by `hard_rule:<name>`.
"""

from __future__ import annotations

import re
from functools import lru_cache

import pathspec
from unidiff import PatchSet, UnidiffParseError

# rule name -> gitignore-style patterns; any touched path matching one is a violation
PATH_RULES: dict[str, list[str]] = {
    "no_policy_edit": [".pr-agent.yml", "CONVENTIONS.md"],
    "no_lint_config_edit": [
        "ruff.toml",
        ".ruff.toml",
        "setup.cfg",
        ".flake8",
        ".bandit",
        ".semgrep/",
        ".semgrepignore",
        ".gitleaks.toml",
        ".gitleaksignore",
        ".gitattributes",  # export-ignore / export-subst change what the scanners see
        "pyproject.toml",
        "conftest.py",
    ],
    "no_codeowners_edit": ["CODEOWNERS"],  # root, .github/ and docs/ (any directory)
    "no_ci_edit": [".github/", ".gitlab-ci.yml", "Jenkinsfile"],
    "no_dependency_change": [
        "requirements*.txt",
        "uv.lock",
        "poetry.lock",
        "Pipfile*",
        "setup.py",
        "pyproject.toml",
    ],
}
TEST_FILES = ["tests/", "test_*.py"]
SKIP_MARKERS = (
    "pytest.mark.skip",  # also covers pytest.mark.skipif
    "pytest.mark.xfail",
    "pytest.skip(",
    "unittest.skip",
)
# Comments that silence a scanner: a "fix" must never be an added suppression.
SUPPRESSIONS = re.compile(
    r"#\s*(?:nosec|noqa|nosemgrep|type:\s*ignore|pragma:\s*no\s*cover)|gitleaks:allow", re.I
)
TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w*)")


@lru_cache(maxsize=64)
def _spec(patterns: tuple[str, ...]) -> pathspec.PathSpec:
    return pathspec.PathSpec.from_lines("gitignore", patterns)


def _matches(path: str, patterns: list[str]) -> bool:
    return _spec(tuple(patterns)).match_file(path)


def is_test_file(path: str) -> bool:
    """Test modules: anything under a `tests/` directory or named `test_*.py`."""
    return _matches(path, TEST_FILES)


def _strip(path: str) -> str:
    return path.removeprefix("a/").removeprefix("b/")


def _path_violations(path: str) -> list[tuple[str, str]]:
    """Violations caused just by touching `path`."""
    if path.startswith("/") or ".." in path.split("/"):
        return [("path_escape", path)]
    return [(rule, path) for rule, patterns in PATH_RULES.items() if _matches(path, patterns)]


def hard_rule_violations(patch: str) -> list[tuple[str, str]]:
    """(rule_name, detail) for every hard rule the unified diff breaks; [] if clean.

    A diff we cannot parse is itself a violation (fail closed).
    """
    try:
        patched = PatchSet(patch)
    except UnidiffParseError as e:
        return [("malformed_patch", str(e))]
    if patch.strip() and not len(patched):
        return [("malformed_patch", "no file headers found")]
    out: list[tuple[str, str]] = []
    removed_tests: set[str] = set()
    added_tests: set[str] = set()
    for pf in patched:
        for raw in (pf.source_file, pf.target_file):
            if raw != "/dev/null":
                out += [v for v in _path_violations(_strip(raw)) if v not in out]
        if pf.is_removed_file and is_test_file(_strip(pf.source_file)):
            out.append(("no_test_deletion", f"deleted {_strip(pf.source_file)}"))
        for hunk in pf:
            for line in hunk:
                test = TEST_DEF.match(line.value)
                if line.is_removed and test:
                    removed_tests.add(test.group(1))
                if line.is_added and test:
                    added_tests.add(test.group(1))
                if line.is_added and any(m in line.value for m in SKIP_MARKERS):
                    out.append(("no_test_skip", f"{pf.path}: {line.value.strip()}"))
                if line.is_added and SUPPRESSIONS.search(line.value):
                    out.append(("no_suppression", f"{pf.path}: {line.value.strip()[:80]}"))
    for name in sorted(removed_tests - added_tests):
        out.append(("no_test_deletion", f"removed {name}"))
    return out
