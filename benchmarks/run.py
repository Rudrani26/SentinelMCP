"""Reproducible direct-vs-gateway Streamable HTTP benchmarks.

Compares, over real loopback Streamable HTTP:

    Direct:  client -> upstream
    Gateway: client -> SentinelMCP -> upstream

Run:

    python -m benchmarks.run

See docs/benchmark-methodology.md for the full methodology (warm-up, trial
count, concurrency levels, metrics, connection/session behavior, and known
limitations). This module implements that methodology; it does not restate
it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import platform
import statistics
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp

from examples.upstream_server import build_server
from sentinelmcp.gateway.limits import ConcurrencyLimiter, RateLimiter
from sentinelmcp.gateway.server import build_gateway_app
from sentinelmcp.policy.models import PolicyConfig
from sentinelmcp.telemetry.audit import AuditLogger

CONCURRENCY_LEVELS: tuple[int, ...] = (1, 10, 25, 50, 100)
TRIALS_PER_CONFIGURATION = 5
NOOP_REQUESTS_PER_CLIENT = 20
FIXED_LATENCY_REQUESTS_PER_CLIENT = 5
FIXED_LATENCY_DURATION_SECONDS = 0.01
DENIED_REQUEST_CONCURRENCY = 10
DENIED_REQUEST_REQUESTS_PER_CLIENT = 10

API_KEY_ENV = "SENTINELMCP_BENCHMARK_API_KEY"
API_KEY = "benchmark-only-not-a-real-secret"
PRINCIPAL = "benchmark-agent"

RESULTS_DIR = Path(__file__).parent / "results"

BENCHMARK_POLICY = PolicyConfig.model_validate(
    {
        "principals": {
            PRINCIPAL: {
                "api_key_env": API_KEY_ENV,
                "tools": {
                    "benchmark.noop": {"effect": "allow"},
                    "benchmark.fixed_latency": {"effect": "allow", "deny_unknown_arguments": False},
                    # "database.execute_write" is deliberately absent: used
                    # for the denied-request-latency sub-benchmark below.
                },
            }
        }
    }
)


# --- statistics --------------------------------------------------------------


@dataclass
class Sample:
    elapsed_seconds: float
    is_error: bool
    is_timeout: bool


@dataclass
class TrialStats:
    request_count: int
    wall_seconds: float
    throughput_rps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    mean_ms: float
    error_rate: float
    timeout_rate: float


@dataclass
class ConfigurationResult:
    path: str  # "direct" | "gateway"
    tool: str
    concurrency: int
    trials: list[TrialStats]
    median: TrialStats
    variability: dict[str, float]


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (the standard definition), stdlib-only."""
    if not sorted_values:
        return float("nan")
    k = (len(sorted_values) - 1) * (pct / 100)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def _compute_trial_stats(samples: list[Sample], wall_seconds: float) -> TrialStats:
    n = len(samples)
    elapsed_ms_sorted = sorted(s.elapsed_seconds * 1000 for s in samples)
    errors = sum(1 for s in samples if s.is_error)
    timeouts = sum(1 for s in samples if s.is_timeout)
    return TrialStats(
        request_count=n,
        wall_seconds=wall_seconds,
        throughput_rps=(n / wall_seconds) if wall_seconds > 0 else float("nan"),
        p50_ms=_percentile(elapsed_ms_sorted, 50),
        p95_ms=_percentile(elapsed_ms_sorted, 95),
        p99_ms=_percentile(elapsed_ms_sorted, 99),
        mean_ms=statistics.fmean(elapsed_ms_sorted) if elapsed_ms_sorted else float("nan"),
        error_rate=(errors / n) if n else float("nan"),
        timeout_rate=(timeouts / n) if n else float("nan"),
    )


_MEDIAN_FIELDS = ("throughput_rps", "p50_ms", "p95_ms", "p99_ms", "mean_ms", "error_rate", "timeout_rate")


def _median_trial_stats(trials: list[TrialStats]) -> tuple[TrialStats, dict[str, float]]:
    """The median of each metric across trials, and each metric's range
    (max - min) across trials as the variability measure."""
    medians = {f: statistics.median(getattr(t, f) for t in trials) for f in _MEDIAN_FIELDS}
    variability = {f: max(getattr(t, f) for t in trials) - min(getattr(t, f) for t in trials) for f in _MEDIAN_FIELDS}
    median = TrialStats(
        request_count=trials[0].request_count,
        wall_seconds=statistics.median(t.wall_seconds for t in trials),
        **medians,
    )
    return median, variability


# --- workload ------------------------------------------------------------------


async def _worker(
    client_factory: Callable[[], AbstractAsyncContextManager[Client]],
    tool_name: str,
    tool_args: dict,
    requests_per_client: int,
) -> list[Sample]:
    samples: list[Sample] = []
    async with client_factory() as client:
        for _ in range(requests_per_client):
            start = time.perf_counter()
            try:
                result = await client.call_tool(tool_name, tool_args)
                samples.append(
                    Sample(elapsed_seconds=time.perf_counter() - start, is_error=result.is_error, is_timeout=False)
                )
            except TimeoutError:
                samples.append(Sample(elapsed_seconds=time.perf_counter() - start, is_error=True, is_timeout=True))
            except Exception:
                samples.append(Sample(elapsed_seconds=time.perf_counter() - start, is_error=True, is_timeout=False))
    return samples


async def _run_trial(
    client_factory: Callable[[], AbstractAsyncContextManager[Client]],
    tool_name: str,
    tool_args: dict,
    concurrency: int,
    requests_per_client: int,
) -> TrialStats:
    """`concurrency` persistent client connections, each issuing
    `requests_per_client` sequential calls over its own session - the same
    connection/session-reuse shape for both direct and gateway runs."""
    start = time.perf_counter()
    results = await asyncio.gather(
        *(_worker(client_factory, tool_name, tool_args, requests_per_client) for _ in range(concurrency))
    )
    wall_seconds = time.perf_counter() - start
    flat = [s for worker_samples in results for s in worker_samples]
    return _compute_trial_stats(flat, wall_seconds)


async def _run_configuration_matrix(
    path_name: str, client_factory: Callable[[], AbstractAsyncContextManager[Client]]
) -> list[ConfigurationResult]:
    results: list[ConfigurationResult] = []
    tool_configs = [
        ("benchmark.noop", {}, NOOP_REQUESTS_PER_CLIENT),
        (
            "benchmark.fixed_latency",
            {"duration_seconds": FIXED_LATENCY_DURATION_SECONDS},
            FIXED_LATENCY_REQUESTS_PER_CLIENT,
        ),
    ]
    for tool_name, tool_args, requests_per_client in tool_configs:
        for concurrency in CONCURRENCY_LEVELS:
            await _run_trial(client_factory, tool_name, tool_args, concurrency, requests_per_client)  # warm-up
            trials = [
                await _run_trial(client_factory, tool_name, tool_args, concurrency, requests_per_client)
                for _ in range(TRIALS_PER_CONFIGURATION)
            ]
            median, variability = _median_trial_stats(trials)
            results.append(
                ConfigurationResult(
                    path=path_name, tool=tool_name, concurrency=concurrency, trials=trials, median=median,
                    variability=variability,
                )
            )
            print(
                f"  {path_name:8s} {tool_name:24s} c={concurrency:4d}  "
                f"p50={median.p50_ms:7.2f}ms  p95={median.p95_ms:7.2f}ms  p99={median.p99_ms:7.2f}ms  "
                f"throughput={median.throughput_rps:8.1f} rps  errors={median.error_rate:.1%}"
            )
    return results


async def _run_denied_request_benchmark(
    client_factory: Callable[[], AbstractAsyncContextManager[Client]],
) -> TrialStats:
    """A representative unauthorized call through the gateway - measures
    pure policy-denial latency (no upstream contact) separately from the
    normal-throughput numbers above. Direct upstream has no analogous
    concept (there is no authorization layer to deny anything)."""
    trial = await _run_trial(
        client_factory,
        "database.execute_write",
        {"database": "x", "statement": "x"},
        DENIED_REQUEST_CONCURRENCY,
        DENIED_REQUEST_REQUESTS_PER_CLIENT,
    )
    print(
        f"  denied-request  database.execute_write   c={DENIED_REQUEST_CONCURRENCY:4d}  "
        f"p50={trial.p50_ms:7.2f}ms  p95={trial.p95_ms:7.2f}ms  p99={trial.p99_ms:7.2f}ms"
    )
    return trial


def _compute_overhead(
    direct_results: list[ConfigurationResult], gateway_results: list[ConfigurationResult]
) -> list[dict]:
    direct_by_key = {(r.tool, r.concurrency): r for r in direct_results}
    overhead = []
    for g in gateway_results:
        d = direct_by_key.get((g.tool, g.concurrency))
        if d is None:
            continue
        absolute_ms = g.median.p50_ms - d.median.p50_ms
        percentage = (absolute_ms / d.median.p50_ms * 100) if d.median.p50_ms else float("nan")
        overhead.append(
            {
                "tool": g.tool,
                "concurrency": g.concurrency,
                "direct_p50_ms": d.median.p50_ms,
                "gateway_p50_ms": g.median.p50_ms,
                "absolute_overhead_ms": absolute_ms,
                "percentage_overhead": percentage,
            }
        )
    return overhead


def _policy_latency_stats_from_audit_log(audit_log_path: Path) -> dict:
    """Policy-evaluation latency, measured separately from total gateway
    overhead, straight from the same audit records the gateway always
    produces (Phase 5) - not a separate instrumentation pass."""
    latencies: list[float] = []
    if audit_log_path.exists():
        for line in audit_log_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            value = record.get("policy_latency_ms")
            if value is not None:
                latencies.append(value)
    if not latencies:
        return {"sample_count": 0}
    latencies.sort()
    return {
        "sample_count": len(latencies),
        "p50_ms": _percentile(latencies, 50),
        "p95_ms": _percentile(latencies, 95),
        "p99_ms": _percentile(latencies, 99),
        "mean_ms": statistics.fmean(latencies),
    }


# --- environment ---------------------------------------------------------------


def _package_version(name: str) -> str | None:
    try:
        return pkg_version(name)
    except PackageNotFoundError:
        return None


def _environment_info() -> dict:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "mcp_version": _package_version("mcp"),
        "uvicorn_version": _package_version("uvicorn"),
        "httpx2_version": _package_version("httpx2"),
        "jsonschema_version": _package_version("jsonschema"),
    }


# --- server lifecycle (self-contained; no dependency on tests/) ---------------


@asynccontextmanager
async def _running_asgi_app(app: ASGIApp, *, mcp_path: str = "/mcp") -> AsyncIterator[str]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        mcp_url = f"http://127.0.0.1:{port}{mcp_path}"
        async with httpx2.AsyncClient() as probe:
            last_exc: Exception | None = None
            for _ in range(50):
                try:
                    await probe.get(mcp_url)
                    break
                except httpx2.HTTPError as exc:
                    last_exc = exc
                    await asyncio.sleep(0.02)
            else:
                # Fail loudly: silently proceeding into a benchmark against an
                # unreachable server produces a wall of per-request connection
                # errors that looks like a saturation finding but is actually
                # a broken run.
                raise RuntimeError(f"{mcp_url} never became reachable") from last_exc
        yield mcp_url
    finally:
        server.should_exit = True
        await serve_task


def _make_direct_client_factory(url: str) -> Callable[[], AbstractAsyncContextManager[Client]]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[Client]:
        async with Client(url, mode="legacy") as client:
            yield client

    return factory


def _make_gateway_client_factory(url: str) -> Callable[[], AbstractAsyncContextManager[Client]]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[Client]:
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {API_KEY}"}) as http_client:
            async with Client(streamable_http_client(url, http_client=http_client), mode="legacy") as client:
                yield client

    return factory


# --- orchestration ---------------------------------------------------------------


async def run_benchmarks() -> dict:
    os.environ[API_KEY_ENV] = API_KEY
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    audit_log_path = RESULTS_DIR / "_last_run_audit.jsonl"
    audit_log_path.unlink(missing_ok=True)

    upstream_app = build_server().streamable_http_app(host="127.0.0.1")
    async with _running_asgi_app(upstream_app) as upstream_url:
        print(f"upstream: {upstream_url}")

        # Rate/concurrency limits set effectively unlimited: this benchmark
        # measures baseline gateway overhead, not Phase 4's deliberate
        # throttling behavior (that's benchmarked, if at all, separately).
        gateway_app = build_gateway_app(
            upstream_url,
            BENCHMARK_POLICY,
            rate_limiter=RateLimiter(capacity=1_000_000.0, refill_rate=1_000_000.0),
            concurrency_limiter=ConcurrencyLimiter(max_concurrent=10_000),
            audit_logger=AuditLogger(audit_log_path),
        )

        # Both MCPServer constructions above call configure_logging(), which
        # installs an INFO-level root handler; a log line per HTTP
        # request/response (from httpx2 and the SDK's own session code) is
        # itself slow enough to dominate the very latencies being measured.
        # Force WARNING *after* construction so a benchmark run measures
        # gateway behavior, not logging overhead.
        logging.getLogger().setLevel(logging.WARNING)
        for noisy_logger in ("httpx2", "mcp", "uvicorn", "uvicorn.access"):
            logging.getLogger(noisy_logger).setLevel(logging.WARNING)

        async with _running_asgi_app(gateway_app) as gateway_url:
            print(f"gateway:  {gateway_url}\n")

            direct_factory = _make_direct_client_factory(upstream_url)
            gateway_factory = _make_gateway_client_factory(gateway_url)

            print("=== direct (client -> upstream) ===")
            direct_results = await _run_configuration_matrix("direct", direct_factory)

            print("\n=== gateway (client -> SentinelMCP -> upstream) ===")
            gateway_results = await _run_configuration_matrix("gateway", gateway_factory)

            print("\n=== denied-request latency (gateway only) ===")
            denied_stats = await _run_denied_request_benchmark(gateway_factory)

        # audit_log_path is fully flushed once the gateway's `async with` block
        # above exits (its lifespan, and every in-flight audit write, has
        # completed) - read it only after that point.
        policy_latency = _policy_latency_stats_from_audit_log(audit_log_path)

    overhead = _compute_overhead(direct_results, gateway_results)

    report = {
        "environment": _environment_info(),
        "configuration": {
            "concurrency_levels": list(CONCURRENCY_LEVELS),
            "trials_per_configuration": TRIALS_PER_CONFIGURATION,
            "noop_requests_per_client": NOOP_REQUESTS_PER_CLIENT,
            "fixed_latency_requests_per_client": FIXED_LATENCY_REQUESTS_PER_CLIENT,
            "fixed_latency_duration_seconds": FIXED_LATENCY_DURATION_SECONDS,
        },
        "direct": [asdict(r) for r in direct_results],
        "gateway": [asdict(r) for r in gateway_results],
        "denied_request_latency": asdict(denied_stats),
        "gateway_overhead": overhead,
        "policy_evaluation_latency": policy_latency,
    }
    return report


def _print_overhead_summary(report: dict) -> None:
    print("\n=== gateway overhead (p50, vs. direct) ===")
    for row in report["gateway_overhead"]:
        print(
            f"  {row['tool']:24s} c={row['concurrency']:4d}  "
            f"direct={row['direct_p50_ms']:7.2f}ms  gateway={row['gateway_p50_ms']:7.2f}ms  "
            f"overhead={row['absolute_overhead_ms']:+7.2f}ms ({row['percentage_overhead']:+6.1f}%)"
        )
    pl = report["policy_evaluation_latency"]
    if pl.get("sample_count"):
        print(
            f"\npolicy-evaluation latency (schema + argument-constraint checks only, from "
            f"{pl['sample_count']} audit records): "
            f"p50={pl['p50_ms']:.2f}ms  p95={pl['p95_ms']:.2f}ms  p99={pl['p99_ms']:.2f}ms"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the direct-vs-gateway SentinelMCP benchmark suite.")
    parser.parse_args()

    report = asyncio.run(run_benchmarks())
    _print_overhead_summary(report)

    output_path = RESULTS_DIR / f"{report['environment']['timestamp'].replace(':', '-')}.json"
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nRaw results written to {output_path}")


if __name__ == "__main__":
    main()
