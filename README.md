# pr-agent: agentic PR review & autofix

The agent improves pull requests: it reviews, fixes, and re-verifies in loops until every finding is resolved. When it can't resolve something safely, it stops and escalates: it asks the author a question or requests a human review.

It is built with **LangGraph**: fan-out reviewers, a propose → critique → verify subgraph with a revise loop, a deterministic policy gate with `interrupt()` for human approval, and SQLite checkpoints for resume. It is traced and evaluated with **LangSmith**. Deterministic analyzers (ruff, bandit, semgrep, gitleaks, pytest) provide the evidence. LLM agents (Claude) find logic bugs and convention violations and propose fixes. **Autonomy boundaries** read from the PR's *base* branch decide, in code, what may be changed automatically.

## Documentation

The docs folder has the full plan. IMPLEMENTATION_PLAN.md covers how the system is built, DESIGN.md covers the production architecture and the trade-offs behind it, and AGENTS.md describes each agent's role, what it gets, what it returns, and its limits.

- [Implementation plan (MVP)](docs/IMPLEMENTATION_PLAN.md): scope, decisions, autonomy boundaries, graph, rule learning, convergence loop, waves, eval cases
- [Design (production architecture)](docs/DESIGN.md): threat model, performance, scalability, production path
- [Agents and roles](docs/AGENTS.md): every LLM agent and deterministic role, with inputs, output schema, permissions, escalation and eval metric

## Quick start

```bash
cp .env.example .env        # add ANTHROPIC_API_KEY and LANGSMITH_API_KEY
uv run pr-agent demo        # builds the demo repo and reviews seeded PR 01 (SQL injection)
```

Other commands:

```bash
uv run pr-agent review --local <repo> --base main --head <branch>
uv run pr-agent review https://github.com/<owner>/<repo>/pull/<n>   # GITHUB_TOKEN optional (read-only)
uv run pr-agent resume <run-id> --approve F1,F3                     # approve `suggest` fixes later
uv run pr-agent init <repo> && uv run pr-agent check-config <repo>  # autonomy boundaries
uv run pr-agent eval --offline                                      # 12 seeded cases, scripted LLM, no keys
uv run pr-agent eval                                                # same cases through LangSmith evaluate()
```

Outputs go to `.pr-agent/runs/<run-id>/`: `review.md`, `review.json` and `patches/`. Fixes that may ship are committed to a local `autofix/<pr>` branch; nothing is ever pushed.

## Status

Last updated: merge of rule learning, the convergence loop (now on by default) and the independent code-review fixes.

### Implemented and tested

Everything below ran in this environment. `ruff check` and `mypy` are clean. `pytest` gives **415 passed** with the convergence loop on (the default), and **413 passed + 2 expected xfails** with `PR_AGENT_CONVERGE=0`; the 2 xfails are fixes that only round 2 can make. The tests use no API keys and no network: a fake/scripted LLM plus the real git, ruff, bandit, semgrep and pytest.

- **Ingest:** local branches (`--local`) and GitHub PR URLs, using REST metadata, a partial clone and `refs/pull/N/head`. URL mode is tested against a mocked API and a local bare repo.
- **Triage and diff parsing:** file filters, docs/config-only PRs.
- **Analyzers:** ruff (with deterministic fixes), bandit, semgrep (vendored offline rules + the base branch's `.semgrep/`).
  - Every config is read from the **base** branch.
  - Inline suppressions in the PR are ignored.
  - Tools run as `python -P -m <tool>`, so a PR-supplied `ruff.py` can't run in place of the tool.
- **Test runner:** pytest, reporting only failures the PR introduced.
- **LLM agents** ([AGENTS.md](docs/AGENTS.md)): bug, security and conventions reviewers, fix_proposer and fix_critic (≤ 2 revisions), and rule_drafter.
  - PR content is wrapped as untrusted, including critic feedback.
  - The LLM can't choose the fix type or the policy level.
- **verify_fix:** the patch applies, the finding is gone, no new blocking findings, and tests pass. New lint is deferred only when another round exists and ruff can fix it.
- **Autonomy boundaries:**
  - The most restrictive of path, route (`ast`), fix type, public API change, lint rule, limits and hard rules.
  - Hard rules include no added suppressions, and no edits to `.gitattributes`/scanner configs.
  - Fail-closed, with `decided_by` on every decision, plus `pr-agent init` / `check-config`.
- **Approval gate:** `interrupt()` with interactive approval. `pr-agent resume` time-travels to the gate and only accepts ids that are actually awaiting approval.
- **Convergence loop (on by default):**
  - Lands fixes on `autofix/<pr>` and re-analyzes the new tip.
  - Stops on no findings, no progress, or `max_rounds` (3).
  - Every open finding is escalated, never dropped: a review request to the owners with evidence, or a concrete question to the author, under "Needs your input".
  - Finding ids stay stable across rounds.
- **Rule learning:**
  - Ladder: (a) enable an existing rule (deterministic), (b) a new semgrep rule, (c) a CONVENTIONS entry.
  - Validated by `verify_rule` (`semgrep --test`, fires on head, silent on the fixed code, base hit count).
  - Critical/high paths get step (a) only, labelled "partially verified (no fixed code)".
  - Output: a "Prevent this next time" section plus `rules/` files.
- **Publishing:** review.md, review.json, patch files, and a local `autofix/<pr>` branch via a temporary worktree.
- **Redaction:** secrets are redacted in all output and in the LangSmith payload hooks: provider keys, Stripe, Google, JWTs, URL credentials, and secret-named assignments.
- **Evals:** 13 seeded cases. The offline run ([evals/results.md](evals/results.md), scripted LLM, loop on) gives:
  - precision 100%, recall 95%, fix pass rate 100% (15/15);
  - policy_correct, owners_tagged, clean_zero, injection_unchanged, config_tamper_ignored, converged, escalations_complete, rule_valid_case01 and no_rules_clean all at 100%.

### Implemented, not verified here

- **Live LLM and LangSmith runs** (`pr-agent demo`, and `pr-agent eval` without `--offline`): no API keys were available yet, so no real model output or LangSmith trace has been observed.
- **Docker sandbox and gitleaks:** only command construction and parsing are unit-tested (no Docker daemon here). Without Docker, the tool falls back to a local temp dir and shows a banner.
- **`--post`** (posting the review to GitHub): mocked API only.

### Designed, not built yet

- **`--rules-branch`:** a separate `rules/<pr>` branch with the proposals ([plan §5, rule learning](docs/IMPLEMENTATION_PLAN.md#rule-learning)). Proposals are written to `.pr-agent/runs/<id>/rules/` today.
- **Generated role cards:** `docs/AGENTS.md` generated from prompt headers and checked by a test. Today it is hand-written.
- **The production path** (GitHub App, webhooks, queue, Postgres, k8s sandbox, feedback loop): [DESIGN.md §9](docs/DESIGN.md#9-production-path-what-the-mvp-leaves-out-and-how-each-piece-comes-back).

### Known issues

- Case 05 recall is 0.67 in the offline eval: one of the three expected lint findings isn't reported, because the scripted case doesn't produce it. Under investigation.
- `src/` is about 5.5k lines including docstrings and blank lines, well above the original 2–2.5k target.
- **Fixed in this release:** the e2e hang (an empty-dict resume value) and the independent code review's confirmed findings:
  - tool shadowing by PR files;
  - `.gitattributes` hiding files from the scanners;
  - LLM-chosen fix types;
  - finding-id reuse across rounds;
  - redaction gaps;
  - honoured suppressions;
  - resume routing;
  - over-eager deferral.

  Regression tests are in `tests/unit/test_review_fixes.py`.

## Development

```bash
uv run ruff check . && uv run mypy && uv run pytest
```

`main` always passes ruff and pytest. Work happens on `dev`, and every merge to `main` updates this Status section.
