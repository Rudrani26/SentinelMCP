"""Structured JSONL audit records: exactly one terminal record per
authenticated, structurally valid `tools/call` attempt, regardless of which
pipeline stage the call exits at.

Redaction (see `represent_arguments`) controls how argument *values* are
represented in the record - never whether one is written at all. The safe
default is `redacted`; `full` exists only for controlled local debugging and
must never be the default (see `DEFAULT_REDACTION_MODE`).

Concurrent writers never interleave or corrupt each other's records: writes
are serialized behind one `asyncio.Lock`, and each write is a single
`str.write()` call of one already-complete JSON line plus a newline.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

RedactionMode = Literal["none", "keys_only", "redacted", "full"]

DEFAULT_REDACTION_MODE: RedactionMode = "redacted"

# Matched as a case-insensitive substring of each argument key, at any
# nesting depth, so "database_password" and "apiKey" are both caught.
SENSITIVE_NAME_FRAGMENTS: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "api_key",
    "authorization",
    "credential",
)

_REDACTED_PLACEHOLDER = "***REDACTED***"


class AuditOutcome(StrEnum):
    """Terminal outcomes for a `tools/call` attempt. Not exhaustive of every
    conceivable failure mode - see `AuditLogger.emit`'s docstring - but every
    outcome CLAUDE.md names explicitly has one, plus `internal_error` for an
    unexpected exception the pipeline itself did not anticipate."""

    RATE_LIMITED = "rate_limited"
    POLICY_DENIED = "policy_denied"
    SCHEMA_REJECTED = "schema_rejected"
    CONCURRENCY_REJECTED = "concurrency_rejected"
    UPSTREAM_SUCCESS = "upstream_success"
    UPSTREAM_ERROR = "upstream_error"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


def _normalize_name(name: str) -> str:
    """Lowercase with `_`/`-` stripped, so "api_key", "apiKey", and
    "api-key" all normalize to the same "apikey" for matching."""
    return re.sub(r"[_-]", "", name.lower())


_NORMALIZED_SENSITIVE_FRAGMENTS: tuple[str, ...] = tuple(_normalize_name(f) for f in SENSITIVE_NAME_FRAGMENTS)


def _is_sensitive_name(name: str) -> bool:
    normalized = _normalize_name(name)
    return any(fragment in normalized for fragment in _NORMALIZED_SENSITIVE_FRAGMENTS)


def _redact(value: Any) -> Any:
    """Recursively replace sensitive-named fields' values; structure and
    non-sensitive values pass through unchanged, at any nesting depth."""
    if isinstance(value, Mapping):
        return {
            str(key): (_REDACTED_PLACEHOLDER if _is_sensitive_name(str(key)) else _redact(val))
            for key, val in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    return value


def _keys_only(value: Any) -> Any:
    """Recursively keep key names/shape, replacing every leaf value."""
    if isinstance(value, Mapping):
        return {str(key): _keys_only(val) for key, val in value.items()}
    if isinstance(value, list | tuple):
        return ["<item>" for _ in value]
    return "<value>"


def represent_arguments(arguments: Mapping[str, Any], mode: RedactionMode) -> Any:
    """How argument values are represented in an audit record, per `mode`:

    - `none`: omitted entirely (`null`).
    - `keys_only`: key names and structure only; every value replaced.
    - `redacted` (default): full structure and values, except sensitive-
      named fields (see `SENSITIVE_NAME_FRAGMENTS`), which are replaced -
      at any nesting depth, so a sensitive value cannot leak through a
      nested object.
    - `full`: everything, completely unredacted. Can expose sensitive data;
      intended only for controlled local debugging, never a production
      default.
    """
    if mode == "none":
        return None
    if mode == "keys_only":
        return _keys_only(arguments)
    if mode == "redacted":
        return _redact(arguments)
    if mode == "full":
        return arguments
    raise ValueError(f"unknown redaction mode: {mode!r}")


@dataclass
class AuditRecord:
    """One terminal record for one `tools/call` attempt.

    `arguments` is stored **raw**; redaction is applied only at write time
    (`AuditLogger.emit`), so a record object in memory (e.g. for tests) is
    never mistaken for what actually reaches disk.
    """

    correlation_id: str
    principal: str
    session_id: str | None
    tool_name: str
    arguments: Mapping[str, Any]
    outcome: AuditOutcome
    matched_rule: str | None
    schema_result: Literal["passed", "failed", "not_evaluated"]
    policy_result: Literal["passed", "failed", "not_evaluated"]
    rate_limit_result: Literal["passed", "failed"]
    concurrency_result: Literal["passed", "failed", "not_evaluated"]
    upstream_attempted: bool
    upstream_result_category: Literal["success", "error", "timeout", "cancelled"] | None
    policy_latency_ms: float | None
    upstream_latency_ms: float | None
    total_latency_ms: float
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def new_correlation_id() -> str:
    return uuid.uuid4().hex


class AuditLogger:
    """Appends one JSON line per `AuditRecord` to a file.

    The blocking file write happens off the event loop
    (`asyncio.to_thread`), but is still serialized behind `self._lock` so
    two concurrent `emit()` calls can never interleave their writes -
    `asyncio.to_thread` alone would not guarantee that, since the OS could
    schedule the two write syscalls in either order or interleave partial
    writes on some platforms/file sizes.
    """

    def __init__(self, path: str | Path, *, redaction_mode: RedactionMode = DEFAULT_REDACTION_MODE) -> None:
        self._path = Path(path)
        self._redaction_mode = redaction_mode
        self._lock = asyncio.Lock()

    def _serialize(self, record: AuditRecord) -> str:
        payload = asdict(record)
        payload["outcome"] = record.outcome.value
        payload["arguments"] = represent_arguments(record.arguments, self._redaction_mode)
        return json.dumps(payload, separators=(",", ":"), default=str)

    def _append_line(self, line: str) -> None:
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)

    async def emit(self, record: AuditRecord) -> None:
        line = self._serialize(record) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append_line, line)


class LatencyTimer:
    """A small monotonic-clock stopwatch, for the record's `*_latency_ms` fields."""

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start) * 1000.0
