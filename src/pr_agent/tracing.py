"""Secret redaction (for traces AND review output) and LangSmith run config."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\blsv2_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{10,}\b"),  # Stripe
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),  # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),  # JWT
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),  # user:password@ in URLs
]
# key = "value" where key looks secret-ish; value redacted if long and high-entropy
_ASSIGN = re.compile(
    r"""(?i)(\w*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|_key)\w*"""
    r"""\s*[:=]\s*['"])([^'"\s]{8,})(['"])"""
)


def redact_text(text: str) -> str:
    for p in _PATTERNS:
        text = p.sub(REDACTED, text)

    def _mask(m: re.Match[str]) -> str:
        # Named like a secret and 8+ chars: redact even low-entropy values ("hunter2hunter2").
        return m.group(1) + REDACTED + m.group(3)

    return _ASSIGN.sub(_mask, text)


def redact(obj: Any) -> Any:
    """Recursively redact strings in dicts/lists (LangSmith hide_inputs/hide_outputs hook)."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact(v) for v in obj]
    return obj


def _redact_payload(payload: dict) -> dict:
    """LangSmith hide_inputs/hide_outputs hook. Pydantic objects are dumped first."""
    return redact(_jsonable(payload))


def _jsonable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    return obj


def tracing_enabled() -> bool:
    import os

    has_key = bool(os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY"))
    return has_key and os.environ.get("LANGSMITH_TRACING", "true").lower() != "false"


def make_tracer(project: str | None = None) -> Any | None:
    """A LangChainTracer whose client redacts every input/output, or None without a key."""
    if not tracing_enabled():
        return None
    import os

    from langchain_core.tracers.langchain import LangChainTracer
    from langsmith import Client

    client = Client(hide_inputs=_redact_payload, hide_outputs=_redact_payload)
    return LangChainTracer(
        client=client, project_name=project or os.environ.get("LANGSMITH_PROJECT", "pr-agent")
    )


def run_config(ctx: Any, thread_id: str, deps: Any, tracer: Any | None) -> dict:
    """LangGraph config: thread for the checkpointer, deps, and LangSmith tags/metadata."""
    return {
        "configurable": {"thread_id": thread_id, "deps": deps},
        "run_name": f"review {ctx.repo_name}#{ctx.pr_id}",
        "tags": [f"repo:{ctx.repo_name}", f"pr:{ctx.pr_id}", f"sha:{ctx.head_sha[:12]}"],
        "metadata": {
            "repo": ctx.repo_name,
            "pr": ctx.pr_id,
            "head_sha": ctx.head_sha,
            "base_sha": ctx.base_sha,
            "sandbox": getattr(deps.sandbox, "mode", "?"),
        },
        "callbacks": [tracer] if tracer else [],
        "recursion_limit": 100,
    }
