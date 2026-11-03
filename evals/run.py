"""Run the seeded demo cases through the full review graph and score them.

    uv run python -m evals.run --offline          # canned LLM outputs, no API keys
    uv run python -m evals.run                    # Anthropic + a LangSmith experiment
    uv run python -m evals.run --offline 09 12    # a subset (name or NN prefix)

Both modes write evals/results.md and return its path.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evals.evaluators import EVALUATORS, Evaluator, Result, summary
from evals.offline import ScriptedLLM
from pr_agent.demo import build, list_cases, load_case
from pr_agent.deps import LLM, Deps
from pr_agent.ingest.local import load_local
from pr_agent.runner import run_review
from pr_agent.settings import Settings

try:
    from pr_agent.sandbox import get_sandbox
except ImportError:  # older layouts: no sandbox selection, local only
    from pr_agent.sandbox import LocalSandbox

    def get_sandbox(settings: Any) -> tuple[Any, list[str]]:  # type: ignore[misc]
        return LocalSandbox(), []


DATASET = "pr-agent-seeded-cases"
RESULTS = Path(__file__).resolve().parent / "results.md"


def approve_none(pending: list[dict[str, Any]]) -> list[str]:
    """Evals never approve suggestions: only AUTO fixes may land."""
    return []


def select_cases(cases: list[str] | None) -> list[Path]:
    """All seeded cases, or those whose dir name equals or starts with one of `cases`."""
    dirs = list_cases()
    if not cases:
        return dirs
    chosen = [d for d in dirs if any(d.name == c or d.name.startswith(c) for c in cases)]
    if not chosen:
        raise ValueError(f"no case matches {cases}")
    return chosen


def run_case(repo: Path, case_dir: Path, llm: LLM, settings: Settings) -> dict[str, Any]:
    """Review one case branch against main and return its review.json."""
    meta = load_case(case_dir)
    sandbox, banners = get_sandbox(settings)
    ctx = load_local(repo, "main", meta["branch"])
    deps = Deps(llm=llm, sandbox=sandbox, settings=settings)
    result = run_review(ctx, deps, approver=approve_none, banners=banners)
    review: dict[str, Any] = json.loads(
        (Path(result.state["output_dir"]) / "review.json").read_text()
    )
    return review


def _offline_rows(repo: Path, dirs: list[Path], settings: Settings) -> list[dict[str, Any]]:
    return [
        {
            "case": d.name,
            "review": run_case(repo, d, ScriptedLLM.for_case(d), settings),
            "expected": load_case(d)["expected"],
        }
        for d in dirs
    ]


# --- LangSmith --------------------------------------------------------------------------


def as_langsmith(fn: Evaluator) -> Callable[[Any, Any], Result]:
    """(run, example) -> {"key", "score", "comment"} for langsmith.evaluate."""

    def evaluator(run: Any, example: Any) -> Result:
        return fn((run.outputs or {}).get("review", {}), example.outputs or {})

    evaluator.__name__ = fn.__name__
    return evaluator


def langsmith_summary(runs: list[Any], examples: list[Any]) -> dict[str, Any]:
    """Micro-averaged precision/recall and fix pass rate over the whole experiment."""
    rows = [
        {
            "case": (ex.inputs or {}).get("case", "?"),
            "review": (run.outputs or {}).get("review", {}),
            "expected": ex.outputs or {},
        }
        for run, ex in zip(runs, examples, strict=True)
    ]
    s = summary(rows)
    return {
        "results": [
            {"key": "micro_precision", "score": s["precision"]},
            {"key": "micro_recall", "score": s["recall"]},
            {"key": "fix_pass_rate", "score": s["fix_pass_rate"]},
        ]
    }


def ensure_dataset(client: Any, dirs: list[Path]) -> Any:
    """Create or update `pr-agent-seeded-cases`: inputs {"case"}, outputs = expected.json."""
    if client.has_dataset(dataset_name=DATASET):
        dataset = client.read_dataset(dataset_name=DATASET)
    else:
        dataset = client.create_dataset(DATASET, description="pr-agent seeded demo PRs")
    existing = {
        (ex.inputs or {}).get("case"): ex for ex in client.list_examples(dataset_id=dataset.id)
    }
    for d in list_cases():
        expected = load_case(d)["expected"]
        ex = existing.get(d.name)
        if ex is None:
            client.create_example(inputs={"case": d.name}, outputs=expected, dataset_id=dataset.id)
        elif (ex.outputs or {}) != expected:
            client.update_example(ex.id, outputs=expected)
    wanted = {d.name for d in dirs}
    return [
        ex
        for ex in client.list_examples(dataset_id=dataset.id)
        if (ex.inputs or {}).get("case") in wanted
    ]


def _online_rows(repo: Path, dirs: list[Path], settings: Settings) -> list[dict[str, Any]]:
    import langsmith
    from langsmith import Client

    from pr_agent.agents.llm import AnthropicLLM

    by_name = {d.name: d for d in dirs}
    llm = AnthropicLLM(settings)

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        return {"review": run_case(repo, by_name[inputs["case"]], llm, settings)}

    client = Client()
    results = langsmith.evaluate(
        target,
        data=ensure_dataset(client, dirs),
        evaluators=[as_langsmith(fn) for fn in EVALUATORS],
        summary_evaluators=[langsmith_summary],  # type: ignore[list-item]  # returns a plain dict
        experiment_prefix="pr-agent",
        max_concurrency=1,
        client=client,
    )
    rows = []
    for r in results:
        run, example = r["run"], r["example"]
        rows.append(
            {
                "case": (example.inputs or {}).get("case", "?"),
                "review": (run.outputs or {}).get("review", {}),
                "expected": example.outputs or {},
            }
        )
    return sorted(rows, key=lambda row: row["case"])


# --- report -----------------------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def render_results(rows: list[dict[str, Any]], mode: str, settings: Settings) -> str:
    s = summary(rows)
    lines = [
        "# pr-agent eval results",
        "",
        f"- Date: {time.strftime('%Y-%m-%d %H:%M')}",
        f"- Mode: {mode}"
        + (
            ""
            if mode == "offline"
            else f" (review {settings.review_model}, fix {settings.fix_model})"
        ),
        "- Convergence loop: "
        + (
            f"on (PR_AGENT_CONVERGE=1, max_rounds={settings.max_rounds})"
            if settings.converge
            else "off (PR_AGENT_CONVERGE=0, single round)"
        ),
        f"- Cases: {s['cases']}",
        "",
        "## Summary",
        "",
        f"- Precision (micro): {_pct(s['precision'])}",
        f"- Recall (micro): {_pct(s['recall'])}",
        f"- Fix pass rate: {_pct(s['fix_pass_rate'])} "
        f"({s['fixes_verified']}/{s['fixes_proposed']} proposed fixes verified)",
        *[f"- {k}: {_pct(v)}" for k, v in s["means"].items()],
        "",
        "## Per case",
        "",
        "Scores are 0-1; `-` means the check does not apply to that case.",
        "",
        s["table"],
        "",
    ]
    return "\n".join(lines)


def main(
    offline: bool = False, cases: list[str] | None = None, results_path: Path | None = None
) -> Path:
    settings = Settings()
    dirs = select_cases(cases)
    repo = build(settings.work_dir / "eval-repo")
    rows = (_offline_rows if offline else _online_rows)(repo, dirs, settings)
    out = results_path or RESULTS
    out.write_text(render_results(rows, "offline" if offline else "online", settings))
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="use canned LLM outputs")
    parser.add_argument("cases", nargs="*", help="case names or NN prefixes (default: all)")
    args = parser.parse_args()
    print(main(offline=args.offline, cases=args.cases or None))
