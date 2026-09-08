"""Bearer API-key authentication.

Resolves `Authorization: Bearer <api-key>` on every downstream HTTP request
to a principal name, and rejects with HTTP 401 - before any MCP
session/message handling occurs - when it cannot. This is plain Bearer-token
validation (as the MCP spec's HTTP transports expect), not OAuth: there is no
token issuance, no authorization server, no scopes.

Real key material is loaded from environment variables at startup; policy
configuration only ever references the environment variable name (see
sentinelmcp/policy/models.py). A presented token is compared as a SHA-256
digest, looked up in a dict, rather than checked in a loop against each
configured principal's raw key: no comparison's timing depends on how many
principals are configured or which one (if any) matches, and the gateway
never holds two raw secrets side by side to compare directly.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
from collections.abc import Generator, Mapping

from mcp_types import INVALID_REQUEST
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

_UNAUTHENTICATED_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": INVALID_REQUEST, "message": "Missing or invalid credentials"},
    }
).encode("utf-8")

_principal_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("sentinelmcp_principal", default=None)


class IdentityConfigError(Exception):
    """An identity configuration problem. Startup must fail closed on this."""


class IdentityResolver:
    """Resolves Bearer tokens to principal names, per a fixed key configuration."""

    def __init__(self, principal_key_env: Mapping[str, str]) -> None:
        digest_to_principal: dict[str, str] = {}
        for principal, env_var in principal_key_env.items():
            key = os.environ.get(env_var)
            if not key:
                raise IdentityConfigError(
                    f"principal {principal!r} references environment variable {env_var!r}, "
                    "which is not set (or is empty)"
                )
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
            existing = digest_to_principal.get(digest)
            if existing is not None:
                # Ambiguous configuration: a presented token could not tell these
                # two principals apart. Fail closed at startup rather than let
                # one principal silently shadow the other.
                raise IdentityConfigError(
                    f"principals {existing!r} and {principal!r} resolve to the same API key "
                    f"(via {env_var!r}); each principal must have a distinct key"
                )
            digest_to_principal[digest] = principal
        self._digest_to_principal = digest_to_principal

    def resolve(self, authorization_values: list[str]) -> str | None:
        """Resolve exactly one Authorization header value to a principal name.

        Fails closed (returns None) when there is no header, more than one
        header (ambiguous), a header that is not exactly `Bearer <token>`, an
        empty token, or a token matching no configured principal.
        """
        if len(authorization_values) != 1:
            return None
        scheme, _, token = authorization_values[0].partition(" ")
        if scheme != "Bearer" or not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return self._digest_to_principal.get(digest)


class BearerAuthMiddleware:
    """ASGI middleware: resolves the caller's principal or rejects with 401.

    Wraps the whole gateway app (not just tool-related routes), so every
    downstream HTTP request in the MCP session - including `initialize` - is
    authenticated before any MCP-level processing occurs. On success, stores
    the resolved principal in a contextvar for the duration of the request,
    readable via `current_principal()`; a caller cannot influence which
    principal is resolved through any request field or header other than a
    valid `Authorization` value, since nothing else is consulted.
    """

    def __init__(self, app: ASGIApp, resolver: IdentityResolver) -> None:
        self._app = app
        self._resolver = resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        header_values = [
            value.decode("latin-1")
            for name, value in scope["headers"]
            if name.decode("latin-1").lower() == "authorization"
        ]
        principal = self._resolver.resolve(header_values)
        if principal is None:
            response = Response(
                content=_UNAUTHENTICATED_BODY,
                status_code=401,
                media_type="application/json",
                headers={"WWW-Authenticate": 'Bearer realm="sentinelmcp"'},
            )
            await response(scope, receive, send)
            return

        token = _principal_var.set(principal)
        try:
            await self._app(scope, receive, send)
        finally:
            _principal_var.reset(token)


def current_principal() -> str:
    """The authenticated principal for the in-flight request.

    Every request reaching gateway handlers has already passed
    `BearerAuthMiddleware`, so this always resolves there; it raises only if
    called outside that middleware's scope, which is a programming error.
    """
    principal = _principal_var.get()
    if principal is None:
        raise RuntimeError("current_principal() called with no authenticated principal in context")
    return principal


@contextlib.contextmanager
def authenticated_as(principal: str) -> Generator[None]:
    """Test seam: run a block as if `BearerAuthMiddleware` had resolved `principal`.

    For unit-testing handlers that call `current_principal()` without going
    through a real HTTP request.
    """
    token = _principal_var.set(principal)
    try:
        yield
    finally:
        _principal_var.reset(token)
