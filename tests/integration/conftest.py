"""Shared integration-test fixtures: one upstream server and one gateway,
shared for the whole test session, rather than one real server pair per
test file.

Running many real uvicorn server pairs back-to-back proved to be a source
of intermittent connection failures specific to this sandboxed environment
(confirmed unrelated to SentinelMCP's own code: every test file passes
reliably in isolation, and reordering test files moved the failure to
whichever one happened to run first - a property of total real-server
churn in one process, not of any particular test's logic). Sharing one
gateway for the whole session is the most direct fix.

Every integration test module that uses `gateway_url` must set
`pytestmark = pytest.mark.asyncio(loop_scope="session")` to run in the same
event loop this fixture is bound to.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from examples.upstream_server import build_server
from sentinelmcp.gateway.server import build_gateway_app
from sentinelmcp.policy.models import PolicyConfig
from tests.support import running_asgi_app

ALLOW_ALL_KEY_ENV = "SENTINELMCP_TEST_ALLOW_ALL_API_KEY"
ALLOW_ALL_KEY = "test-only-allow-all-key"

DIAG_KEY_ENV = "SENTINELMCP_TEST_DIAG_API_KEY"
DIAG_KEY = "test-only-diagnostics-key"

READONLY_KEY_ENV = "SENTINELMCP_TEST_READONLY_API_KEY"
READONLY_KEY = "test-only-readonly-key"

CONSTRAINED_KEY_ENV = "SENTINELMCP_TEST_CONSTRAINED_API_KEY"
CONSTRAINED_KEY = "test-only-constrained-key"

MALFORMED_INPUT_KEY_ENV = "SENTINELMCP_TEST_MALFORMED_INPUT_API_KEY"
MALFORMED_INPUT_KEY = "test-only-malformed-input-key"

ALL_UPSTREAM_TOOL_NAMES = {
    "database.query_stats",
    "database.execute_write",
    "benchmark.noop",
    "benchmark.fixed_latency",
}

SHARED_POLICY = PolicyConfig.model_validate(
    {
        "principals": {
            "allow-all-agent": {
                "api_key_env": ALLOW_ALL_KEY_ENV,
                "tools": {name: {"effect": "allow"} for name in ALL_UPSTREAM_TOOL_NAMES},
            },
            "diagnostics-agent": {
                "api_key_env": DIAG_KEY_ENV,
                "tools": {
                    "database.query_stats": {"effect": "allow"},
                    "benchmark.noop": {"effect": "allow"},
                    "benchmark.fixed_latency": {"effect": "allow"},
                    "database.execute_write": {"effect": "deny"},
                },
            },
            "readonly-agent": {
                "api_key_env": READONLY_KEY_ENV,
                "tools": {"benchmark.noop": {"effect": "allow"}},
            },
            "constrained-diagnostics-agent": {
                "api_key_env": CONSTRAINED_KEY_ENV,
                "tools": {
                    "database.query_stats": {
                        "effect": "allow",
                        # "include_query_text" is required *by policy* even though it
                        # has a Python default and so isn't required by the upstream's
                        # own schema - isolates the policy-level `required` check.
                        "required": ["database", "limit", "include_query_text"],
                        "arguments": {
                            "database": {"in": ["staging", "production"]},
                            "limit": {"min": 1, "max": 100},
                            "include_query_text": {"equals": False},
                        },
                    },
                },
            },
            "malformed-input-agent": {
                "api_key_env": MALFORMED_INPUT_KEY_ENV,
                "tools": {"benchmark.noop": {"effect": "allow"}},
            },
        }
    }
)


@pytest.fixture(scope="session", autouse=True)
def _shared_api_key_env() -> AsyncIterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(ALLOW_ALL_KEY_ENV, ALLOW_ALL_KEY)
        mp.setenv(DIAG_KEY_ENV, DIAG_KEY)
        mp.setenv(READONLY_KEY_ENV, READONLY_KEY)
        mp.setenv(CONSTRAINED_KEY_ENV, CONSTRAINED_KEY)
        mp.setenv(MALFORMED_INPUT_KEY_ENV, MALFORMED_INPUT_KEY)
        yield


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def gateway_url(
    _shared_api_key_env: None, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[str]:
    audit_log_path = tmp_path_factory.mktemp("sentinelmcp-audit") / "audit.jsonl"
    upstream = build_server().streamable_http_app(host="127.0.0.1")
    async with running_asgi_app(upstream) as upstream_url:
        gateway = build_gateway_app(upstream_url, SHARED_POLICY, audit_log_path=str(audit_log_path))
        async with running_asgi_app(gateway) as url:
            yield url
