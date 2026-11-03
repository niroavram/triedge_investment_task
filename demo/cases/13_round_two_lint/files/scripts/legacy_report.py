"""Describe a customer for the legacy report."""

from typing import Final, Optional

UNKNOWN: Final = "unknown"


def describe(name: Optional[str]) -> str:
    return name or UNKNOWN
