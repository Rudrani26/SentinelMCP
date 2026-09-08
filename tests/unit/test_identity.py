"""Unit tests for IdentityResolver: digest-based key resolution and
fail-closed behavior for missing/malformed/unknown/ambiguous credentials."""

from __future__ import annotations

import pytest

from sentinelmcp.gateway.identity import IdentityConfigError, IdentityResolver


def test_resolves_valid_bearer_token_to_correct_principal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KEY_A", "secret-a")
    monkeypatch.setenv("KEY_B", "secret-b")
    resolver = IdentityResolver({"principal-a": "KEY_A", "principal-b": "KEY_B"})

    assert resolver.resolve(["Bearer secret-a"]) == "principal-a"
    assert resolver.resolve(["Bearer secret-b"]) == "principal-b"


def test_missing_header_fails_closed():
    resolver = IdentityResolver({})
    assert resolver.resolve([]) is None


@pytest.mark.parametrize(
    "header_value",
    ["Bearer", "Bearer ", "Basic dGVzdDp0ZXN0", "bearer secret-a", "secret-a", ""],
)
def test_malformed_header_fails_closed(monkeypatch: pytest.MonkeyPatch, header_value: str):
    monkeypatch.setenv("KEY_A", "secret-a")
    resolver = IdentityResolver({"principal-a": "KEY_A"})
    assert resolver.resolve([header_value]) is None


def test_multiple_authorization_headers_fail_closed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KEY_A", "secret-a")
    resolver = IdentityResolver({"principal-a": "KEY_A"})
    assert resolver.resolve(["Bearer secret-a", "Bearer secret-a"]) is None


def test_unknown_token_fails_closed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KEY_A", "secret-a")
    resolver = IdentityResolver({"principal-a": "KEY_A"})
    assert resolver.resolve(["Bearer not-the-configured-key"]) is None


def test_missing_env_var_fails_closed_at_construction(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KEY_MISSING", raising=False)
    with pytest.raises(IdentityConfigError):
        IdentityResolver({"principal-a": "KEY_MISSING"})


def test_empty_env_var_fails_closed_at_construction(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KEY_EMPTY", "")
    with pytest.raises(IdentityConfigError):
        IdentityResolver({"principal-a": "KEY_EMPTY"})


def test_two_principals_sharing_a_key_fails_closed_at_construction(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KEY_SHARED_A", "same-secret")
    monkeypatch.setenv("KEY_SHARED_B", "same-secret")
    with pytest.raises(IdentityConfigError):
        IdentityResolver({"principal-a": "KEY_SHARED_A", "principal-b": "KEY_SHARED_B"})
