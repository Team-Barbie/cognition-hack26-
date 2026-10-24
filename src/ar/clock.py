"""Shared UTC timestamps for persisted runtime records."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
