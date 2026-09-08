"""Unit tests for benchmarks/run.py's statistics helpers - pure functions
whose correctness a benchmark report silently depends on."""

from __future__ import annotations

from benchmarks.run import (
    ConfigurationResult,
    Sample,
    TrialStats,
    _compute_overhead,
    _compute_trial_stats,
    _median_trial_stats,
    _percentile,
)


def test_percentile_of_empty_sequence_is_nan():
    assert _percentile([], 50) != _percentile([], 50)  # NaN != NaN


def test_percentile_p50_of_odd_length_is_the_middle_value():
    assert _percentile([1.0, 2.0, 3.0], 50) == 2.0


def test_percentile_p0_and_p100_are_the_extremes():
    values = [1.0, 5.0, 10.0, 20.0]
    assert _percentile(values, 0) == 1.0
    assert _percentile(values, 100) == 20.0


def test_percentile_interpolates_between_ranks():
    # Standard linear-interpolation percentile: for 5 sorted values,
    # p50 = index (5-1)*0.5 = 2.0 -> the 3rd value exactly.
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert _percentile(values, 50) == 30.0
    # p75 = index (5-1)*0.75 = 3.0 -> the 4th value exactly.
    assert _percentile(values, 75) == 40.0


def test_compute_trial_stats_counts_errors_and_timeouts_separately():
    samples = [
        Sample(elapsed_seconds=0.01, is_error=False, is_timeout=False),
        Sample(elapsed_seconds=0.02, is_error=True, is_timeout=False),
        Sample(elapsed_seconds=0.03, is_error=True, is_timeout=True),
        Sample(elapsed_seconds=0.04, is_error=False, is_timeout=False),
    ]
    stats = _compute_trial_stats(samples, wall_seconds=1.0)
    assert stats.request_count == 4
    assert stats.error_rate == 0.5
    assert stats.timeout_rate == 0.25
    assert stats.throughput_rps == 4.0


def test_compute_trial_stats_throughput_is_request_count_over_wall_time():
    samples = [Sample(elapsed_seconds=0.01, is_error=False, is_timeout=False) for _ in range(10)]
    stats = _compute_trial_stats(samples, wall_seconds=2.0)
    assert stats.throughput_rps == 5.0


def _trial(p50: float, p95: float, p99: float, throughput: float, wall: float = 1.0) -> TrialStats:
    return TrialStats(
        request_count=10,
        wall_seconds=wall,
        throughput_rps=throughput,
        p50_ms=p50,
        p95_ms=p95,
        p99_ms=p99,
        mean_ms=p50,
        error_rate=0.0,
        timeout_rate=0.0,
    )


def test_median_trial_stats_takes_the_median_of_each_metric_independently():
    trials = [_trial(10, 20, 30, 100), _trial(12, 22, 32, 90), _trial(14, 24, 34, 110)]
    median, variability = _median_trial_stats(trials)
    assert median.p50_ms == 12
    assert median.p95_ms == 22
    assert median.p99_ms == 32
    assert median.throughput_rps == 100
    assert variability["p50_ms"] == 4  # max(14) - min(10)


def test_compute_overhead_matches_configurations_by_tool_and_concurrency():
    direct = [
        ConfigurationResult(
            path="direct", tool="benchmark.noop", concurrency=1,
            trials=[_trial(10, 20, 30, 100)], median=_trial(10, 20, 30, 100), variability={},
        )
    ]
    gateway = [
        ConfigurationResult(
            path="gateway", tool="benchmark.noop", concurrency=1,
            trials=[_trial(15, 25, 35, 80)], median=_trial(15, 25, 35, 80), variability={},
        )
    ]
    overhead = _compute_overhead(direct, gateway)
    assert len(overhead) == 1
    assert overhead[0]["tool"] == "benchmark.noop"
    assert overhead[0]["concurrency"] == 1
    assert overhead[0]["absolute_overhead_ms"] == 5.0
    assert overhead[0]["percentage_overhead"] == 50.0


def test_compute_overhead_skips_configurations_with_no_direct_counterpart():
    gateway = [
        ConfigurationResult(
            path="gateway", tool="benchmark.noop", concurrency=999,
            trials=[_trial(15, 25, 35, 80)], median=_trial(15, 25, 35, 80), variability={},
        )
    ]
    assert _compute_overhead([], gateway) == []
