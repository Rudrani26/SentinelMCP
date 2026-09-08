# Policy Semantics

This document describes the behavior implemented by `sentinelmcp/policy/`.
It describes what is actually implemented, not planned functionality.

## Default-deny, exact-name tool authorization

- Tool names are matched exactly. There are no wildcard or regex tool
  selectors, and therefore no rule-precedence system.
- If a principal has no rule at all for a tool, the call is **denied**.
- `effect: deny` denies the call. This is behaviorally identical to having no
  rule at all for that exact tool name - it exists purely for configuration
  readability (e.g. to document "this is deliberately blocked," not just
  absent).
- `effect: allow` does **not** immediately authorize the call. It means:
  continue to upstream `inputSchema` validation, then SentinelMCP argument-
  constraint evaluation. Both must also pass.
- An unknown principal is denied, the same as an unauthorized tool.

A denied call - whether from no rule, `effect: deny`, an unknown principal,
or the upstream not actually having the tool - always produces the exact
same response shape as calling a genuinely nonexistent tool
(`"Unknown tool: <name>"`, `is_error=true`). Hiding a tool from `tools/list`
is not a security boundary (every call is independently re-authorized), but
it is deliberately also not an *information* boundary: a caller cannot tell
"exists but you're not authorized" apart from "doesn't exist."

## Argument validation pipeline

For an allowed tool, in order:

1. **Upstream schema validation** - the caller's arguments are validated
   against the upstream tool's own `inputSchema`, using a standards-compliant
   JSON Schema validator. A malformed or unsupported upstream schema is
   handled safely (reported as a validation failure, not raised) so one
   broken upstream tool cannot crash the gateway.
2. **SentinelMCP argument-constraint evaluation** - the *same, uncoerced*
   arguments are evaluated against the tool's configured constraints (below).
3. Only if both pass are the arguments forwarded upstream - **unchanged**.
   Neither stage mutates the arguments it inspects.

Schema-invalid and argument-policy-invalid results are worded
distinguishably from each other (`"Schema-invalid arguments for ..."` vs.
`"Argument policy violation for ..."`), and both are distinguishable from an
authorization denial (`"Unknown tool: ..."`) and from whatever the upstream
tool itself returns.

## Argument constraint operators

Configured per tool, under `arguments: { <name-or-path>: { ... } }`:

| Operator | Applies to | Meaning |
|---|---|---|
| `equals` | any | Value must strictly equal the configured value |
| `in` | any | Value must strictly equal one of the configured values |
| `min` | number | Value must be >= the configured minimum |
| `max` | number | Value must be <= the configured maximum |
| `min_length` | string | String length must be >= the configured minimum |
| `max_length` | string | String length must be <= the configured maximum |
| `min_items` | list | List length must be >= the configured minimum |
| `max_items` | list | List length must be <= the configured maximum |
| `pattern` | string | Value must fully match the regex (anchored: the whole value, not a substring) |

Plus, at the tool level (not per-argument):

- `required: [name, ...]` - these top-level argument names must be present.
- `forbidden: [name, ...]` - these top-level argument names must be absent.
- `deny_unknown_arguments` (default `true`) - any top-level argument name not
  covered by `required`, `forbidden`, or a key in `arguments` is rejected.

A constraint under `arguments` only applies **when the field is present**.
To mandate presence, use `required`. This keeps "optional but constrained
when given" (e.g. `include_query_text: {equals: false}`, which a caller may
simply omit) distinct from "must be present."

Unknown constraint operators, unknown `ToolPolicy`/`PrincipalPolicy`/
`PolicyConfig` fields, and unknown top-level `ArgumentConstraint` fields are
all configuration errors: every model uses `extra="forbid"`, so a typo or
invented operator prevents the gateway from starting rather than being
silently ignored.

## Nested paths

An `arguments` key may be a dot-separated path (e.g. `options.mode`) to
constrain a value nested inside an object argument. Resolution walks the
path segment by segment; if any segment is missing, or an intermediate
value isn't itself an object, the path is treated as **not present** (the
constraint is skipped, not violated) - the same "applies only when present"
rule as a top-level field. `required`/`forbidden`/`deny_unknown_arguments`
only look at top-level names; a dotted key in `arguments` still marks its
first segment as a "known" top-level field for `deny_unknown_arguments`
purposes.

## Strict type semantics

Constraint evaluation never coerces values, and it treats JSON's distinct
types as genuinely distinct - even where Python's own `==` would not:

```text
100     != "100"     (int vs string)
100     != 100.0      (int vs float)
True    != 1          (bool vs int)
False   != 0          (bool vs int)
```

`equals` and `in` compare by `type(a) is type(b) and a == b`, so a
value can only equal a configured value of the *same* JSON type. `min`/`max`
require the value to genuinely be a number (`int` or `float`, explicitly
excluding `bool`, since Python's `bool` is a subclass of `int`); a numeric-
looking string like `"50"` does not satisfy a numeric constraint. This is
also why the gateway does not use permissive Pydantic coercion on tool
arguments: the value a policy evaluates must be the exact value forwarded.

## Startup validation

Policy files are loaded once, at gateway startup, via
`sentinelmcp.policy.models.load_policy`:

- Invalid YAML prevents startup (`PolicyLoadError`).
- A non-mapping top-level document prevents startup.
- Any structural or field-level validation failure prevents startup -
  including an unrecognized field anywhere in the document, an unrecognized
  constraint operator, an invalid `effect` value, or an invalid (does not
  compile) regular expression in `pattern`.

There is no hot policy reload; a policy change requires restarting the
gateway.
