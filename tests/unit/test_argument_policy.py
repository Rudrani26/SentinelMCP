"""Unit tests for argument-constraint evaluation (equals/in/min/max/
min_length/max_length/min_items/max_items/pattern, required/forbidden,
unknown-field rejection, nested paths) and upstream schema validation.

Strict-type-semantics cases are the specific attacks CLAUDE.md's threat
model calls out (type confusion, nested bypass attempts): `evaluate_argument_
policy` and `validate_input_schema` must never coerce, and a value's JSON
type must match exactly for equals/in to consider it equal.
"""

from __future__ import annotations

import copy

from hypothesis import given
from hypothesis import strategies as st

from sentinelmcp.policy.engine import evaluate_argument_policy, validate_input_schema
from sentinelmcp.policy.models import ToolPolicy

# --- equals / in: strict type semantics ------------------------------------


def _policy(**kwargs) -> ToolPolicy:
    return ToolPolicy.model_validate({"effect": "allow", **kwargs})


def test_equals_matches_same_type_and_value():
    policy = _policy(arguments={"x": {"equals": 100}})
    assert evaluate_argument_policy(policy, {"x": 100}) is None


def test_equals_rejects_int_vs_string():
    policy = _policy(arguments={"x": {"equals": 100}})
    assert evaluate_argument_policy(policy, {"x": "100"}) is not None


def test_equals_rejects_int_vs_float():
    policy = _policy(arguments={"x": {"equals": 100}})
    assert evaluate_argument_policy(policy, {"x": 100.0}) is not None


def test_equals_rejects_bool_vs_int_one():
    policy = _policy(arguments={"x": {"equals": 1}})
    assert evaluate_argument_policy(policy, {"x": True}) is not None


def test_equals_rejects_bool_vs_int_zero():
    policy = _policy(arguments={"x": {"equals": 0}})
    assert evaluate_argument_policy(policy, {"x": False}) is not None


def test_equals_false_accepts_only_boolean_false():
    policy = _policy(arguments={"x": {"equals": False}})
    assert evaluate_argument_policy(policy, {"x": False}) is None
    assert evaluate_argument_policy(policy, {"x": 0}) is not None
    assert evaluate_argument_policy(policy, {"x": "false"}) is not None


def test_equals_null_matches_only_null():
    policy = _policy(arguments={"x": {"equals": None}})
    assert evaluate_argument_policy(policy, {"x": None}) is None
    assert evaluate_argument_policy(policy, {"x": 0}) is not None
    assert evaluate_argument_policy(policy, {"x": ""}) is not None


def test_in_rejects_type_confused_candidate():
    policy = _policy(arguments={"x": {"in": ["staging", "production"]}})
    assert evaluate_argument_policy(policy, {"x": "staging"}) is None
    # A list/dict masquerading as a scalar must not slip through.
    assert evaluate_argument_policy(policy, {"x": ["staging"]}) is not None
    assert evaluate_argument_policy(policy, {"x": {"$ne": None}}) is not None


# --- min/max: strict numeric semantics --------------------------------------


def test_min_max_accept_in_range_numbers():
    policy = _policy(arguments={"limit": {"min": 1, "max": 100}})
    assert evaluate_argument_policy(policy, {"limit": 1}) is None
    assert evaluate_argument_policy(policy, {"limit": 100}) is None
    assert evaluate_argument_policy(policy, {"limit": 50.5}) is None


def test_min_max_reject_out_of_range():
    policy = _policy(arguments={"limit": {"min": 1, "max": 100}})
    assert evaluate_argument_policy(policy, {"limit": 0}) is not None
    assert evaluate_argument_policy(policy, {"limit": 101}) is not None


def test_min_max_reject_numeric_looking_string():
    policy = _policy(arguments={"limit": {"min": 1, "max": 100}})
    assert evaluate_argument_policy(policy, {"limit": "50"}) is not None


def test_min_max_reject_boolean():
    policy = _policy(arguments={"limit": {"min": 0, "max": 1}})
    assert evaluate_argument_policy(policy, {"limit": True}) is not None


# --- string length / pattern ------------------------------------------------


def test_string_length_bounds():
    policy = _policy(arguments={"name": {"min_length": 2, "max_length": 4}})
    assert evaluate_argument_policy(policy, {"name": "ab"}) is None
    assert evaluate_argument_policy(policy, {"name": "abcd"}) is None
    assert evaluate_argument_policy(policy, {"name": "a"}) is not None
    assert evaluate_argument_policy(policy, {"name": "abcde"}) is not None


def test_pattern_is_anchored_to_the_whole_value():
    policy = _policy(arguments={"id": {"pattern": r"[a-z]+-[0-9]+"}})
    assert evaluate_argument_policy(policy, {"id": "abc-123"}) is None
    # A substring match must not be enough - pattern is fullmatch, not search.
    assert evaluate_argument_policy(policy, {"id": "xxabc-123yy"}) is not None


def test_pattern_rejects_non_string():
    policy = _policy(arguments={"id": {"pattern": r"[a-z]+"}})
    assert evaluate_argument_policy(policy, {"id": 123}) is not None


# --- list length -------------------------------------------------------------


def test_list_length_bounds():
    policy = _policy(arguments={"tags": {"min_items": 1, "max_items": 2}})
    assert evaluate_argument_policy(policy, {"tags": ["a"]}) is None
    assert evaluate_argument_policy(policy, {"tags": ["a", "b"]}) is None
    assert evaluate_argument_policy(policy, {"tags": []}) is not None
    assert evaluate_argument_policy(policy, {"tags": ["a", "b", "c"]}) is not None


# --- required / forbidden / unknown-field rejection -------------------------


def test_required_field_missing_is_rejected():
    policy = _policy(required=["database"])
    assert evaluate_argument_policy(policy, {}) is not None
    assert evaluate_argument_policy(policy, {"database": "staging"}) is None


def test_forbidden_field_present_is_rejected():
    policy = _policy(forbidden=["admin_override"])
    assert evaluate_argument_policy(policy, {"admin_override": True}) is not None
    assert evaluate_argument_policy(policy, {}) is None


def test_unknown_field_rejected_by_default():
    policy = _policy(arguments={"database": {}})
    assert evaluate_argument_policy(policy, {"database": "staging", "sneaky": 1}) is not None


def test_unknown_field_allowed_when_disabled():
    policy = _policy(deny_unknown_arguments=False, arguments={"database": {}})
    assert evaluate_argument_policy(policy, {"database": "staging", "extra": 1}) is None


def test_constraint_only_applies_when_field_present():
    """Not in `required` - a caller may simply omit it; the constraint only
    binds when the field is given."""
    policy = _policy(arguments={"include_query_text": {"equals": False}})
    assert evaluate_argument_policy(policy, {}) is None
    assert evaluate_argument_policy(policy, {"include_query_text": False}) is None
    assert evaluate_argument_policy(policy, {"include_query_text": True}) is not None


# --- nested paths -------------------------------------------------------------


def test_nested_path_constraint():
    policy = _policy(arguments={"options.mode": {"equals": "safe"}})
    assert evaluate_argument_policy(policy, {"options": {"mode": "safe"}}) is None
    assert evaluate_argument_policy(policy, {"options": {"mode": "unsafe"}}) is not None


def test_nested_path_bypass_attempt_is_not_present_not_a_violation():
    """An attacker who omits the nested object entirely, or replaces it with a
    non-object, does not satisfy the constraint but also doesn't crash
    evaluation - `required` is the mechanism for mandating presence."""
    policy = _policy(arguments={"options.mode": {"equals": "safe"}})
    assert evaluate_argument_policy(policy, {}) is None
    assert evaluate_argument_policy(policy, {"options": "not-an-object"}) is None
    assert evaluate_argument_policy(policy, {"options": {}}) is None


def test_dotted_argument_key_marks_its_top_level_segment_known():
    policy = _policy(arguments={"options.mode": {"equals": "safe"}})
    # "options" itself is not flagged as an unknown top-level field.
    assert evaluate_argument_policy(policy, {"options": {"mode": "safe"}}) is None


# --- policy evaluation never mutates its input ------------------------------


def test_evaluation_does_not_mutate_arguments():
    policy = _policy(
        required=["database"],
        arguments={"database": {"in": ["staging", "production"]}, "limit": {"min": 1, "max": 100}},
    )
    arguments = {"database": "staging", "limit": 50, "nested": {"a": [1, 2, 3]}}
    before = copy.deepcopy(arguments)
    evaluate_argument_policy(policy, arguments)
    assert arguments == before


@given(
    value=st.one_of(
        st.integers(),
        st.floats(allow_nan=False),
        st.text(),
        st.booleans(),
        st.none(),
        st.lists(st.integers(), max_size=3),
        st.dictionaries(st.text(max_size=3), st.integers(), max_size=3),
    )
)
def test_evaluation_never_mutates_arbitrary_argument_values(value):
    """Property: whatever shape/type an argument value takes, evaluating a
    constraint against it never changes the value - the core "authorized
    value == forwarded value" invariant, generated across type-confused and
    boundary inputs rather than hand-picked ones."""
    policy = _policy(arguments={"x": {"equals": "sentinel-value-that-wont-match"}})
    arguments = {"x": value}
    before = copy.deepcopy(arguments)
    evaluate_argument_policy(policy, arguments)
    assert arguments == before


# --- upstream schema validation ---------------------------------------------


def test_schema_validation_accepts_conforming_arguments():
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": ["limit"]}
    assert validate_input_schema(schema, {"limit": 5}) is None


def test_schema_validation_rejects_wrong_type():
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}
    assert validate_input_schema(schema, {"limit": "five"}) is not None


def test_schema_validation_rejects_missing_required_field():
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": ["limit"]}
    assert validate_input_schema(schema, {}) is not None


def test_schema_validation_handles_malformed_schema_safely():
    """A broken upstream inputSchema must not crash the gateway - it's
    reported as a validation failure like any other."""
    malformed_schema = {"type": "not-a-real-json-schema-type"}
    result = validate_input_schema(malformed_schema, {"limit": 5})
    assert result is not None


def test_schema_validation_none_schema_is_permissive():
    assert validate_input_schema(None, {"anything": "goes"}) is None
