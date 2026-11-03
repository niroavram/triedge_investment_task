from pathlib import Path

import pytest

from pr_agent.learning.verify_rule import (
    CONVENTION_NOTE,
    flag_noise,
    semgrep_test,
    verify_convention,
    verify_existing,
    verify_semgrep,
)
from pr_agent.models import Finding, RuleProposal
from pr_agent.sandbox import LocalSandbox

RULE = """rules:
  - id: SEC-001
    message: SQL built with an f-string
    languages: [python]
    severity: ERROR
    pattern: $CONN.execute(f"...")
"""
TEST_OK = """def bad(conn, x):
    # ruleid: SEC-001
    return conn.execute(f"SELECT * FROM t WHERE x = '{x}'")


def good(conn, x):
    # ok: SEC-001
    return conn.execute("SELECT * FROM t WHERE x = ?", (x,))
"""
# The ok example is annotated as a match: semgrep --test must fail.
TEST_BROKEN = TEST_OK.replace("# ok: SEC-001", "# ruleid: SEC-001")

HEAD = """def find(conn, name):
    rows = conn.execute(f"SELECT * FROM users WHERE name = '{name}'")
    return rows.fetchall()
"""
FIXED = """def find(conn, name):
    rows = conn.execute("SELECT * FROM users WHERE name = ?", (name,))
    return rows.fetchall()
"""
FINDING = Finding(
    id="F1", source="llm:security", category="security", file="app.py", line=2, message="sqli"
)
SANDBOX = LocalSandbox()


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def _base(root: Path, hits: int) -> Path:
    """A base checkout with `hits` existing f-string queries."""
    body = "".join(
        f'def q{i}(conn, v):\n    return conn.execute(f"SELECT {{v}} FROM t{i}")\n\n\n'
        for i in range(hits)
    )
    return _tree(root / "base", {"legacy.py": body or "X = 1\n"})


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    head = _tree(tmp_path / "head", {"app.py": HEAD})
    fixed = _tree(tmp_path / "fixed", {"app.py": FIXED})
    return head, fixed


def _semgrep_proposal(test: str = TEST_OK, rule: str = RULE) -> RuleProposal:
    return RuleProposal(
        finding_id="F1",
        kind="semgrep_rule",
        rule_id="custom:SEC-001",
        content=rule,
        test_content=test,
    )


def test_semgrep_test_passes_and_fails() -> None:
    assert semgrep_test(SANDBOX, RULE, TEST_OK, "SEC-001")[0]
    assert not semgrep_test(SANDBOX, RULE, TEST_BROKEN, "SEC-001")[0]


def test_semgrep_rule_fires_silent_and_counts_base(dirs: tuple[Path, Path], tmp_path: Path):
    head, fixed = dirs
    out = verify_semgrep(_semgrep_proposal(), FINDING, SANDBOX, head, fixed, _base(tmp_path, 2))
    assert out.valid and out.base_hits == 2 and "would also flag" not in out.note


def test_semgrep_rule_broken_test_is_invalid(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    head, fixed = dirs
    proposal = _semgrep_proposal(TEST_BROKEN)
    out = verify_semgrep(proposal, FINDING, SANDBOX, head, fixed, _base(tmp_path, 0))
    assert not out.valid and "semgrep --test failed" in out.note


def test_semgrep_rule_still_firing_on_fix_is_invalid(dirs: tuple[Path, Path], tmp_path: Path):
    head, _ = dirs
    still = _tree(tmp_path / "still", {"app.py": HEAD})  # "fix" that changed nothing
    out = verify_semgrep(_semgrep_proposal(), FINDING, SANDBOX, head, still, _base(tmp_path, 0))
    assert not out.valid and "still fires" in out.note


def test_semgrep_rule_missing_the_finding_is_invalid(dirs: tuple[Path, Path], tmp_path: Path):
    head, fixed = dirs
    far = FINDING.model_copy(update={"line": 30})
    out = verify_semgrep(_semgrep_proposal(), far, SANDBOX, head, fixed, _base(tmp_path, 0))
    assert not out.valid and "does not fire" in out.note


def test_more_than_five_base_hits_adds_note(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    head, fixed = dirs
    out = verify_semgrep(_semgrep_proposal(), FINDING, SANDBOX, head, fixed, _base(tmp_path, 7))
    assert out.valid and out.base_hits == 7
    assert out.note.endswith("would also flag 7 existing places")


def test_existing_ruff_rule_checks(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    head, fixed = dirs
    proposal = RuleProposal(finding_id="F1", kind="enable_rule", rule_id="ruff:S608", content="")
    out = verify_existing(proposal, FINDING, SANDBOX, head, fixed, _base(tmp_path, 3))
    assert out.valid and out.base_hits == 3
    # Without fixed code (critical paths) only "fires" and base hits are checked.
    empty = _base(tmp_path / "b", 0)
    assert verify_existing(proposal, FINDING, SANDBOX, head, None, empty).valid
    assert not verify_existing(proposal, FINDING, SANDBOX, head, head, empty).valid


def test_existing_bandit_rule_checks(dirs: tuple[Path, Path], tmp_path: Path) -> None:
    head, fixed = dirs
    proposal = RuleProposal(finding_id="F1", kind="enable_rule", rule_id="bandit:B608", content="")
    out = verify_existing(proposal, FINDING, SANDBOX, head, fixed, _base(tmp_path, 1))
    assert out.valid and out.base_hits == 1


def test_flag_noise_keeps_existing_note() -> None:
    p = RuleProposal(
        finding_id="F1", kind="enable_rule", rule_id="ruff:S608", content="", base_hits=9,
        note="partially verified (no fixed code)",
    )  # fmt: skip
    assert flag_noise(p).note == (
        "partially verified (no fixed code) — would also flag 9 existing places"
    )
    assert flag_noise(p.model_copy(update={"base_hits": 5})).note == p.note


def _conv(conv_id: str = "CONV-006") -> RuleProposal:
    return RuleProposal(finding_id="F1", kind="convention", rule_id=conv_id, content="## x")


BAD, GOOD = "x = f'{a}'", "x = '{}'.format(a)"


def test_convention_valid() -> None:
    out = verify_convention(_conv(), BAD, GOOD, {"CONV-001", "CONV-005"})
    assert out.valid and out.note == CONVENTION_NOTE


@pytest.mark.parametrize(
    ("conv_id", "bad", "good", "reason"),
    [
        ("CONV-005", BAD, GOOD, "already exists"),
        ("CONV-6", BAD, GOOD, "malformed"),
        ("CONV-006", "def (:", GOOD, "does not parse"),
        ("CONV-006", BAD, f"  {BAD}  ", "identical"),
    ],
)
def test_convention_invalid(conv_id: str, bad: str, good: str, reason: str) -> None:
    out = verify_convention(_conv(conv_id), bad, good, {"CONV-001", "CONV-005"})
    assert not out.valid and reason in out.note
