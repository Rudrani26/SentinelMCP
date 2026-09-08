"""Unit tests for policy YAML loading: malformed configuration must prevent
startup (raise PolicyLoadError), never silently ignore or partially load."""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinelmcp.policy.models import PolicyConfig, PolicyLoadError, load_policy


def _write(tmp_path: Path, content: str) -> str:
    path = tmp_path / "policy.yaml"
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_loads_valid_policy(tmp_path: Path):
    path = _write(
        tmp_path,
        """
        principals:
          agent:
            api_key_env: SOME_ENV_VAR
            tools:
              some.tool:
                effect: allow
        """,
    )
    policy = load_policy(path)
    assert isinstance(policy, PolicyConfig)
    assert policy.principals["agent"].tools["some.tool"].effect == "allow"


def test_missing_file_fails_closed():
    with pytest.raises(PolicyLoadError):
        load_policy("/no/such/file.yaml")


def test_invalid_yaml_fails_closed(tmp_path: Path):
    path = _write(tmp_path, "principals: [this is not: valid: yaml")
    with pytest.raises(PolicyLoadError):
        load_policy(path)


def test_non_mapping_top_level_fails_closed(tmp_path: Path):
    path = _write(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(PolicyLoadError):
        load_policy(path)


def test_unknown_top_level_field_fails_closed(tmp_path: Path):
    path = _write(
        tmp_path,
        """
        principals:
          agent:
            api_key_env: SOME_ENV_VAR
        unexpected_field: true
        """,
    )
    with pytest.raises(PolicyLoadError):
        load_policy(path)


def test_unknown_tool_policy_field_fails_closed(tmp_path: Path):
    path = _write(
        tmp_path,
        """
        principals:
          agent:
            api_key_env: SOME_ENV_VAR
            tools:
              some.tool:
                effect: allow
                min: 1
        """,
    )
    with pytest.raises(PolicyLoadError):
        load_policy(path)


def test_unknown_effect_value_fails_closed(tmp_path: Path):
    path = _write(
        tmp_path,
        """
        principals:
          agent:
            api_key_env: SOME_ENV_VAR
            tools:
              some.tool:
                effect: maybe
        """,
    )
    with pytest.raises(PolicyLoadError):
        load_policy(path)


def test_loads_full_argument_constraint_set(tmp_path: Path):
    path = _write(
        tmp_path,
        """
        principals:
          agent:
            api_key_env: SOME_ENV_VAR
            tools:
              some.tool:
                effect: allow
                required: [database, limit]
                forbidden: [admin_override]
                deny_unknown_arguments: true
                arguments:
                  database:
                    in: [staging, production]
                  limit:
                    min: 1
                    max: 100
                  name:
                    min_length: 1
                    max_length: 50
                  tags:
                    min_items: 0
                    max_items: 10
                  id:
                    pattern: "[a-z]+-[0-9]+"
                  options.mode:
                    equals: safe
        """,
    )
    policy = load_policy(path)
    tool = policy.principals["agent"].tools["some.tool"]
    assert tool.required == ["database", "limit"]
    assert tool.arguments["database"].in_ == ["staging", "production"]
    assert tool.arguments["options.mode"].equals == "safe"


def test_unknown_constraint_operator_fails_closed(tmp_path: Path):
    path = _write(
        tmp_path,
        """
        principals:
          agent:
            api_key_env: SOME_ENV_VAR
            tools:
              some.tool:
                effect: allow
                arguments:
                  database:
                    regex: "staging|production"
        """,
    )
    with pytest.raises(PolicyLoadError):
        load_policy(path)


def test_invalid_regex_pattern_fails_closed(tmp_path: Path):
    path = _write(
        tmp_path,
        """
        principals:
          agent:
            api_key_env: SOME_ENV_VAR
            tools:
              some.tool:
                effect: allow
                arguments:
                  id:
                    pattern: "["
        """,
    )
    with pytest.raises(PolicyLoadError):
        load_policy(path)


def test_bundled_example_policy_loads():
    """The example.yaml shipped in the repo must itself be valid policy - it's
    what the README/demo tells a reader to look at."""
    example_path = Path(__file__).parents[2] / "sentinelmcp" / "policy" / "policies" / "example.yaml"
    policy = load_policy(str(example_path))
    assert set(policy.principals) == {"diagnostics-agent", "readonly-agent"}


def test_missing_api_key_env_fails_closed(tmp_path: Path):
    path = _write(
        tmp_path,
        """
        principals:
          agent:
            tools:
              some.tool:
                effect: allow
        """,
    )
    with pytest.raises(PolicyLoadError):
        load_policy(path)
