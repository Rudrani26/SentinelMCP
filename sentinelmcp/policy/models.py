"""Pydantic models and loader for SentinelMCP's policy configuration.

Every model forbids unknown fields (`extra="forbid"`), so an unrecognized
policy field, or an unrecognized argument-constraint operator, is a
configuration error caught at load time - not silently ignored. See
docs/policy-semantics.md for the full semantics this shape implements.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ArgumentConstraint(BaseModel):
    """A constraint on one argument (or nested argument path).

    Every field is optional; only the operators actually set are enforced,
    combined with AND. Whether `equals`/`in` were explicitly configured is
    read from `model_fields_set`, not by checking for `None`, since `None`
    (JSON `null`) is itself a value a constraint might legitimately target.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    equals: Any = None
    in_: list[Any] | None = Field(default=None, alias="in")
    min: float | None = None
    max: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    min_items: int | None = None
    max_items: int | None = None
    pattern: str | None = None

    @field_validator("pattern")
    @classmethod
    def _pattern_must_compile(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regular expression {value!r}: {exc}") from exc
        return value


class ToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect: Literal["allow", "deny"]
    required: list[str] = []
    forbidden: list[str] = []
    deny_unknown_arguments: bool = True
    arguments: dict[str, ArgumentConstraint] = {}


class PrincipalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key_env: str
    tools: dict[str, ToolPolicy] = {}


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principals: dict[str, PrincipalPolicy]


class PolicyLoadError(Exception):
    """Malformed policy YAML or structure. Startup must fail closed on this."""


def load_policy(path: str) -> PolicyConfig:
    """Load and validate a policy file. Raises PolicyLoadError on any problem."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as exc:
        raise PolicyLoadError(f"could not read policy file {path!r}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PolicyLoadError(f"invalid YAML in {path!r}: {exc}") from exc

    if not isinstance(raw, dict):
        raise PolicyLoadError(f"policy file {path!r} must contain a YAML mapping at the top level")

    try:
        return PolicyConfig.model_validate(raw)
    except ValidationError as exc:
        raise PolicyLoadError(f"invalid policy structure in {path!r}: {exc}") from exc
