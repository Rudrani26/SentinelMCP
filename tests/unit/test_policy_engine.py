"""Unit tests for the policy engine's default-deny, exact-name authorization
semantics."""

from __future__ import annotations

from sentinelmcp.policy.engine import is_tool_allowed
from sentinelmcp.policy.models import PolicyConfig

POLICY = PolicyConfig.model_validate(
    {
        "principals": {
            "agent": {
                "api_key_env": "SOME_ENV_VAR",
                "tools": {
                    "allowed.tool": {"effect": "allow"},
                    "denied.tool": {"effect": "deny"},
                },
            }
        }
    }
)


def test_allowed_tool_is_allowed():
    assert is_tool_allowed(POLICY, "agent", "allowed.tool") is True


def test_explicitly_denied_tool_is_denied():
    assert is_tool_allowed(POLICY, "agent", "denied.tool") is False


def test_tool_with_no_rule_is_denied_by_default():
    assert is_tool_allowed(POLICY, "agent", "unmentioned.tool") is False


def test_unknown_principal_is_denied():
    assert is_tool_allowed(POLICY, "no-such-agent", "allowed.tool") is False
