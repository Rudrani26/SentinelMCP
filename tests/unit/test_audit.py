"""Unit tests for audit redaction and JSONL writing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sentinelmcp.telemetry.audit import (
    SENSITIVE_NAME_FRAGMENTS,
    AuditLogger,
    AuditOutcome,
    AuditRecord,
    new_correlation_id,
    represent_arguments,
)


def _record(**overrides) -> AuditRecord:
    defaults = dict(
        correlation_id="corr-1",
        principal="agent",
        session_id="session-1",
        tool_name="database.query_stats",
        arguments={"database": "staging", "limit": 5},
        outcome=AuditOutcome.UPSTREAM_SUCCESS,
        matched_rule="database.query_stats",
        schema_result="passed",
        policy_result="passed",
        rate_limit_result="passed",
        concurrency_result="passed",
        upstream_attempted=True,
        upstream_result_category="success",
        policy_latency_ms=1.2,
        upstream_latency_ms=3.4,
        total_latency_ms=5.6,
    )
    defaults.update(overrides)
    return AuditRecord(**defaults)


# --- redaction modes ----------------------------------------------------------


def test_redaction_none_omits_arguments_entirely():
    assert represent_arguments({"database": "staging", "password": "hunter2"}, "none") is None


def test_redaction_keys_only_keeps_structure_hides_values():
    result = represent_arguments({"database": "staging", "nested": {"limit": 5}}, "keys_only")
    assert result == {"database": "<value>", "nested": {"limit": "<value>"}}


def test_redaction_redacted_hides_only_sensitive_fields():
    result = represent_arguments({"database": "staging", "api_key": "sk-secret"}, "redacted")
    assert result["database"] == "staging"
    assert result["api_key"] == "***REDACTED***"


def test_redaction_redacted_is_recursive_through_nested_objects():
    """A non-sensitive parent key doesn't hide its own recursion: only the
    sensitively-named leaf inside it is redacted."""
    result = represent_arguments({"options": {"password": "hunter2", "username": "alice"}}, "redacted")
    assert result["options"]["password"] == "***REDACTED***"
    assert result["options"]["username"] == "alice"


def test_redaction_redacted_hides_the_whole_value_when_the_key_itself_is_sensitive():
    """A sensitively-named key (e.g. "credentials") has its entire value
    replaced, even if some nested sub-field wouldn't otherwise be sensitive -
    the safer, more conservative choice than cherry-picking sub-fields."""
    result = represent_arguments({"credentials": {"username": "alice", "password": "hunter2"}}, "redacted")
    assert result["credentials"] == "***REDACTED***"


def test_redaction_redacted_is_recursive_through_lists_of_objects():
    result = represent_arguments({"items": [{"token": "abc"}, {"safe": "value"}]}, "redacted")
    assert result["items"][0]["token"] == "***REDACTED***"
    assert result["items"][1]["safe"] == "value"


@pytest.mark.parametrize(
    "sensitive_key",
    ["password", "PASSWORD", "db_password", "secret", "client_secret", "token", "auth_token", "api_key", "apiKey",
     "Authorization", "credential", "credentials"],
)
def test_redaction_catches_all_documented_sensitive_name_fragments(sensitive_key: str):
    result = represent_arguments({sensitive_key: "sensitive-value"}, "redacted")
    assert result[sensitive_key] == "***REDACTED***"


def test_redaction_full_exposes_everything_unchanged():
    arguments = {"password": "hunter2", "database": "staging"}
    result = represent_arguments(arguments, "full")
    assert result == arguments


def test_redaction_unknown_mode_raises():
    with pytest.raises(ValueError):
        represent_arguments({}, "bogus-mode")  # type: ignore[arg-type]


# --- property-based: redaction never leaks, at any nesting depth or casing --

_SENSITIVE_KEY_SPELLINGS = st.sampled_from(SENSITIVE_NAME_FRAGMENTS).flatmap(
    lambda fragment: st.sampled_from(
        [fragment, fragment.upper(), fragment.replace("_", ""), f"my_{fragment}", f"{fragment}_value"]
    )
)


@st.composite
def _structure_with_one_sensitive_leaf(draw: st.DrawFn) -> tuple[dict, str, str]:
    """A dict with a sensitively-named key (some documented fragment, in some
    casing/spelling variant) and a safe key, wrapped in 0-4 layers of
    unrelated nesting - generated so the exact secret and safe values are
    known and can be checked for after redaction."""
    secret_value = draw(st.uuids()).hex
    safe_value = draw(st.uuids()).hex
    sensitive_key = draw(_SENSITIVE_KEY_SPELLINGS)
    depth = draw(st.integers(min_value=0, max_value=4))

    node: dict = {sensitive_key: secret_value, "safe_field": safe_value}
    for _ in range(depth):
        node = {"wrapper": node}
    return node, secret_value, safe_value


@given(_structure_with_one_sensitive_leaf())
def test_redacted_mode_never_leaks_the_sensitive_value_at_any_nesting_depth_or_spelling(
    data: tuple[dict, str, str],
) -> None:
    """Property: for any documented sensitive-name fragment, in any casing or
    common spelling variant, at any nesting depth, its value never survives
    into the "redacted" representation - while an unrelated safe value at
    the same depth does (ruling out a trivial "redact everything" pass)."""
    structure, secret_value, safe_value = data
    serialized = json.dumps(represent_arguments(structure, "redacted"))
    assert secret_value not in serialized
    assert safe_value in serialized


# --- correlation IDs ----------------------------------------------------------


def test_correlation_ids_are_unique():
    ids = {new_correlation_id() for _ in range(100)}
    assert len(ids) == 100


# --- AuditLogger: default mode, secrets, concurrency ------------------------


async def test_default_redaction_mode_is_redacted(tmp_path: Path):
    logger = AuditLogger(tmp_path / "audit.jsonl")
    await logger.emit(_record(arguments={"api_key": "sk-should-not-appear"}))

    line = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip()
    assert "sk-should-not-appear" not in line
    assert json.loads(line)["arguments"]["api_key"] == "***REDACTED***"


async def test_emitted_record_contains_expected_fields(tmp_path: Path):
    logger = AuditLogger(tmp_path / "audit.jsonl")
    await logger.emit(_record())

    line = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["correlation_id"] == "corr-1"
    assert payload["principal"] == "agent"
    assert payload["tool_name"] == "database.query_stats"
    assert payload["outcome"] == "upstream_success"
    assert "timestamp" in payload


async def test_concurrent_emits_never_interleave_or_corrupt(tmp_path: Path):
    logger = AuditLogger(tmp_path / "audit.jsonl")

    async def emit_one(i: int) -> None:
        await logger.emit(_record(correlation_id=f"corr-{i}", arguments={"n": i}))

    await asyncio.gather(*(emit_one(i) for i in range(200)))

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 200
    correlation_ids = set()
    for line in lines:
        payload = json.loads(line)  # raises if any line is corrupted/interleaved JSON
        correlation_ids.add(payload["correlation_id"])
    assert len(correlation_ids) == 200
