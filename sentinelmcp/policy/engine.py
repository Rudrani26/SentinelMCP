"""Authorization decisions: default-deny tool policy, upstream schema
validation, and SentinelMCP argument-constraint evaluation.

See docs/policy-semantics.md for the full semantics. Summary:

- no matching tool rule for this principal -> deny
- `effect: deny` -> deny (behaviorally identical to no rule at all; it
  exists only for configuration readability)
- `effect: allow` -> schema validation, then argument-constraint evaluation
- unknown principal -> deny
- neither schema validation nor argument constraints ever mutate the
  arguments they inspect; the value evaluated is the value forwarded
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import jsonschema
from jsonschema.exceptions import SchemaError, ValidationError

from sentinelmcp.policy.models import ArgumentConstraint, PolicyConfig, ToolPolicy


def resolve_tool_policy(policy: PolicyConfig, principal: str, tool_name: str) -> ToolPolicy | None:
    """The tool's policy iff `principal` is explicitly allowed to call it.

    Returns `None` for every denial reason - unknown principal, no rule for
    this tool, or an explicit `effect: deny` - so callers cannot distinguish
    them (see `resolve_tool_policy`'s callers for why that's deliberate).
    """
    principal_policy = policy.principals.get(principal)
    if principal_policy is None:
        return None
    tool_policy = principal_policy.tools.get(tool_name)
    if tool_policy is None or tool_policy.effect != "allow":
        return None
    return tool_policy


def is_tool_allowed(policy: PolicyConfig, principal: str, tool_name: str) -> bool:
    """Whether `principal` is authorized to call `tool_name`, per policy."""
    return resolve_tool_policy(policy, principal, tool_name) is not None


def validate_input_schema(input_schema: Mapping[str, Any] | None, arguments: Mapping[str, Any]) -> str | None:
    """Validate `arguments` against the upstream tool's own `inputSchema`.

    Returns a human-readable violation reason, or `None` if the arguments
    conform. A malformed/unsupported upstream schema is handled safely: it is
    reported as a violation reason rather than raised, so one broken upstream
    tool schema cannot crash the gateway.
    """
    if input_schema is None:
        return None
    try:
        jsonschema.validate(instance=arguments, schema=input_schema)
    except ValidationError as exc:
        return exc.message
    except SchemaError as exc:
        return f"upstream inputSchema is invalid: {exc.message}"
    except Exception as exc:  # noqa: BLE001 - an upstream schema jsonschema can't even evaluate must not crash us
        return f"upstream inputSchema could not be evaluated: {exc}"
    return None


def _strict_equal(a: Any, b: Any) -> bool:
    """Equality that never crosses JSON type boundaries.

    Plain `==` treats `100 == 100.0` and `True == 1` as equal; authorization
    must not (see docs/policy-semantics.md's strict-type-semantics section).
    `type(a) is type(b)` rejects those before `==` ever runs.
    """
    return type(a) is type(b) and a == b


def _strict_in(value: Any, candidates: Sequence[Any]) -> bool:
    return any(_strict_equal(value, candidate) for candidate in candidates)


def _resolve_path(arguments: Mapping[str, Any], path: str) -> tuple[Any, bool]:
    """Navigate a dot-separated path into `arguments`.

    Returns `(value, True)` if the full path resolves, else `(None, False)` -
    a missing key or a non-dict encountered partway through both mean "not
    present", not an error: constraints on a path only apply when it exists
    (use `required` to mandate presence).
    """
    current: Any = arguments
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return None, False
        current = current[segment]
    return current, True


def _known_top_level_fields(tool_policy: ToolPolicy) -> set[str]:
    known = set(tool_policy.required) | set(tool_policy.forbidden)
    known.update(path.split(".", 1)[0] for path in tool_policy.arguments)
    return known


def _check_constraint(path: str, value: Any, constraint: ArgumentConstraint) -> str | None:
    fields_set = constraint.model_fields_set
    if "equals" in fields_set and not _strict_equal(value, constraint.equals):
        return f"{path!r} does not equal the configured value"
    if "in_" in fields_set and not _strict_in(value, constraint.in_ or []):
        return f"{path!r} is not one of the allowed values"
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if constraint.min is not None and not (is_number and value >= constraint.min):
        return f"{path!r} is below the configured minimum"
    if constraint.max is not None and not (is_number and value <= constraint.max):
        return f"{path!r} is above the configured maximum"
    if constraint.min_length is not None and not (isinstance(value, str) and len(value) >= constraint.min_length):
        return f"{path!r} is shorter than the configured minimum length"
    if constraint.max_length is not None and not (isinstance(value, str) and len(value) <= constraint.max_length):
        return f"{path!r} is longer than the configured maximum length"
    if constraint.min_items is not None and not (isinstance(value, list) and len(value) >= constraint.min_items):
        return f"{path!r} has fewer than the configured minimum items"
    if constraint.max_items is not None and not (isinstance(value, list) and len(value) <= constraint.max_items):
        return f"{path!r} has more than the configured maximum items"
    if constraint.pattern is not None and not (isinstance(value, str) and re.fullmatch(constraint.pattern, value)):
        return f"{path!r} does not match the configured pattern"
    return None


def evaluate_argument_policy(tool_policy: ToolPolicy, arguments: Mapping[str, Any]) -> str | None:
    """Evaluate `arguments` against `tool_policy`'s constraints.

    Returns a human-readable violation reason, or `None` if they're
    satisfied. Never mutates `arguments`; every value inspected here is
    exactly the value the caller sent.
    """
    for name in tool_policy.required:
        if name not in arguments:
            return f"missing required field {name!r}"

    for name in tool_policy.forbidden:
        if name in arguments:
            return f"forbidden field {name!r} was present"

    if tool_policy.deny_unknown_arguments:
        known = _known_top_level_fields(tool_policy)
        for name in arguments:
            if name not in known:
                return f"unknown field {name!r}"

    for path, constraint in tool_policy.arguments.items():
        value, present = _resolve_path(arguments, path)
        if not present:
            continue
        reason = _check_constraint(path, value, constraint)
        if reason is not None:
            return reason

    return None
