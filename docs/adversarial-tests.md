# Adversarial Test Categories

Each category below maps to the security property it proves, and to the
tests that prove it. This is the "one-sentence security purpose" table
CLAUDE.md's Phase 6 asks for. The full invariant-to-test mapping (all 18
numbered invariants from CLAUDE.md's Security Invariants section) follows
in the next table.

| Category | Security purpose | Representative tests |
|---|---|---|
| Hidden-tool direct invocation | A tool absent from discovery is still independently denied on direct call - hiding is not authorization. | `test_hidden_tool_direct_invocation_is_denied_and_never_reaches_upstream` |
| Unauthorized invocation never reaches upstream | A denied call has zero side effects on the protected upstream tool. | `test_call_tool_denies_without_reaching_upstream`, `test_hidden_tool_direct_invocation_is_denied_and_never_reaches_upstream` (counter proof) |
| Unknown principal | A principal with no policy entry fails closed, identically to a denied one. | `test_call_tool_denies_for_unknown_principal_without_reaching_upstream`, `test_unknown_principal_is_denied` |
| Unknown tool | A tool name with no rule, or one the upstream doesn't actually have, fails closed. | `test_call_tool_denies_tool_with_no_policy_rule_without_reaching_upstream`, `test_call_tool_denies_when_upstream_does_not_actually_have_the_tool` |
| Nested unknown arguments | An extra field nested inside a known object argument is caught by schema validation even where the (deliberately top-level-only) policy engine does not catch it. | `test_nested_unknown_field_is_a_documented_scope_limit_of_deny_unknown_arguments`, `test_schema_validation_catches_a_nested_unknown_field_the_policy_engine_does_not` |
| String/number/boolean type confusion | `100 != "100"`, `100 != 100.0`, `True != 1`, `False != 0` for every operator - no coercion. | `test_equals_rejects_int_vs_string`, `test_equals_rejects_int_vs_float`, `test_equals_rejects_bool_vs_int_one/zero`, `test_min_max_reject_numeric_looking_string`, `test_min_max_reject_boolean`, plus the Hypothesis property test |
| Malformed schemas | A broken upstream `inputSchema` is reported as a failure, not raised - one bad tool schema can't crash the gateway. | `test_schema_validation_handles_malformed_schema_safely` |
| Malformed policies | Invalid YAML, non-mapping documents, and invalid field values all prevent startup. | `tests/unit/test_policy_models.py` (11 tests) |
| Unknown policy operators | An unrecognized constraint operator name is a configuration error, not a silently-ignored no-op. | `test_unknown_constraint_operator_fails_closed` |
| Concurrency races | 100+ (unit) / real concurrent HTTP (integration) attempts against a small ceiling never exceed it. | `test_hostile_concurrent_load_never_exceeds_the_ceiling`, `test_concurrency_ceiling_enforced_under_real_concurrent_load` |
| Cancellation races | Cancelling an in-flight call releases its concurrency slot exactly once, never leaking or double-releasing capacity. | `test_acquire_context_manager_releases_on_cancellation`, `test_concurrency_capacity_is_released_after_cancellation`, `test_cancellation_during_upstream_call_emits_one_cancelled_record` |
| Token-bucket boundaries | Burst-to-capacity, refill rate, capacity clamping, and fractional refill all match the documented math exactly, via a deterministic fake clock. | `test_bucket_starts_full_and_allows_a_burst_up_to_capacity`, `test_bucket_refill_never_exceeds_capacity`, `test_bucket_fractional_refill_is_tracked_precisely` |
| Recursive sensitive-data redaction | A sensitive-named field is redacted at any nesting depth, in any casing/spelling variant, under the default mode. | `test_redaction_redacted_is_recursive_through_nested_objects`, `test_redacted_mode_never_leaks_the_sensitive_value_at_any_nesting_depth_or_spelling` (Hypothesis) |
| Concurrent audit writes | Many simultaneous audit emissions never interleave or corrupt each other's JSON lines. | `test_concurrent_emits_never_interleave_or_corrupt` (200, unit), `test_concurrent_calls_produce_valid_non_interleaved_audit_records` (20, real HTTP) |
| Upstream timeout | A gateway-enforced timeout on the upstream call is a distinct, audited outcome, and releases concurrency capacity. | `test_concurrency_capacity_is_released_after_timeout`, `test_upstream_timeout_emits_one_upstream_timeout_record` |
| Upstream failure | An upstream exception is caught and returned to the caller as a protocol-correct `CallToolResult` error (not re-raised into the caller's own transport stream), is a distinct audited outcome, and releases concurrency capacity. | `test_concurrency_capacity_is_released_after_upstream_exception`, `test_upstream_exception_emits_one_upstream_error_record_and_returns_gracefully`, `test_gateway_bridges_representative_upstream_error`, `test_upstream_process_dying_mid_call_does_not_hang_and_maps_to_a_graceful_error` |
| Upstream connection disappearing mid-call | A real OS-level upstream process death while a call is in flight surfaces promptly (not an indefinite hang), as a graceful error, with the concurrency slot released and the gateway still able to serve other principals/tools afterward. | `test_upstream_process_dying_mid_call_does_not_hang_and_maps_to_a_graceful_error` |
| Malformed envelopes | Invalid JSON, non-JSON-RPC bodies, unsupported methods, wrong param types, and deeply nested garbage never crash or hang the gateway. | `tests/integration/test_malformed_input.py` (12 tests) |
| Repeated/malformed request identifiers | A non-string/number/null id, or two requests reusing the same id, don't crash or hang the gateway. | `test_malformed_request_id_type_does_not_crash_the_gateway`, `test_repeated_request_id_does_not_crash_the_gateway` |

## The 18 numbered security invariants

Every numbered invariant from CLAUDE.md's Security Invariants section,
mapped to at least one automated test that proves it. Test names are exact
and can be run individually, e.g. `pytest -k test_unknown_principal_is_denied`.

| # | Invariant | Proven by |
|---|---|---|
| 1 | No request reaches a protected tool without an authenticated principal. | `test_missing_header_fails_closed` (unit), `test_missing_credentials_rejected` (integration) |
| 2 | No tool absent from the principal's allow policy can execute. | `test_tool_with_no_rule_is_denied_by_default` |
| 3 | Hiding a tool from `tools/list` is not treated as authorization. | `test_hidden_tool_direct_invocation_is_denied_and_never_reaches_upstream` |
| 4 | Every `tools/call` is independently authorized. | `test_hidden_tool_direct_invocation_is_denied_and_never_reaches_upstream`, `test_explicit_deny_behaves_like_default_deny_and_never_reaches_upstream` |
| 5 | Values evaluated by policy are the same values forwarded upstream. | `test_permitted_constrained_call_succeeds_with_values_forwarded_unchanged` (end-to-end, reflected values), `test_evaluation_does_not_mutate_arguments` (unit) |
| 6 | Security validation does not coerce strings into numbers, booleans, or other privileged types. | `test_equals_rejects_int_vs_string`, `test_min_max_reject_numeric_looking_string`, `test_min_max_reject_boolean` |
| 7 | Malformed policy configuration prevents startup. | `test_invalid_yaml_fails_closed`, `test_non_mapping_top_level_fails_closed` |
| 8 | Unknown principals fail closed. | `test_unknown_principal_is_denied` (engine), `test_unknown_credentials_rejected` (integration) |
| 9 | Unknown tools fail closed. | `test_call_tool_denies_tool_with_no_policy_rule_without_reaching_upstream`, `test_call_tool_denies_when_upstream_does_not_actually_have_the_tool` |
| 10 | Unknown constraint operators fail closed at configuration time. | `test_unknown_constraint_operator_fails_closed` |
| 11 | Unknown policy fields fail closed. | `test_unknown_top_level_field_fails_closed`, `test_unknown_tool_policy_field_fails_closed` |
| 12 | A denied call never reaches the upstream tool. | `test_hidden_tool_direct_invocation_is_denied_and_never_reaches_upstream` (counter proof), `test_call_tool_denies_without_reaching_upstream` |
| 13 | Concurrent upstream execution never exceeds the configured ceiling. | `test_hostile_concurrent_load_never_exceeds_the_ceiling` (unit, 150 attempts), `test_concurrency_ceiling_enforced_under_real_concurrent_load` (real HTTP) |
| 14 | Cancellation, timeout, and exceptions release concurrency capacity. | `test_acquire_context_manager_releases_on_cancellation`, `test_acquire_context_manager_releases_on_exception`, `test_concurrency_capacity_is_released_after_timeout` |
| 15 | Sensitive values are not included in audit records under default configuration. | `test_secrets_never_appear_in_the_audit_log_under_the_default_redaction_mode`, `test_redaction_redacted_hides_only_sensitive_fields` |
| 16 | Every authenticated, structurally valid `tools/call` attempt produces exactly one terminal audit record. | The `test_*_emits_one_*_record` family: `test_successful_call_emits_one_upstream_success_record_with_matching_correlation_id`, `test_policy_denied_call_emits_one_policy_denied_record`, `test_schema_rejected_call_emits_one_schema_rejected_record`, `test_rate_limited_call_emits_one_rate_limited_record`, `test_concurrency_rejected_call_emits_one_concurrency_rejected_record`, `test_upstream_exception_emits_one_upstream_error_record_and_returns_gracefully`, `test_upstream_timeout_emits_one_upstream_timeout_record`, `test_cancellation_during_upstream_call_emits_one_cancelled_record` |
| 17 | Upstream failures are distinguishable from authentication, schema, policy, rate-limit, and concurrency failures. | `test_every_failure_category_produces_a_pairwise_distinct_audit_outcome` (its docstring names this invariant explicitly) |
| 18 | Authorization evaluation never mutates tool argument values. | `test_evaluation_does_not_mutate_arguments`, `test_evaluation_never_mutates_arbitrary_argument_values` (Hypothesis) |
