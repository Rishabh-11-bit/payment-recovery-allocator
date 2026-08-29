"""Real timing of the real pipeline. What's pinned here is the mechanism --
that ingest and decision are timed separately, over heterogeneous cases,
against the actual worker path -- not any specific millisecond figure, which
is machine-dependent by nature and would make this test flaky by design if
it asserted a threshold.
"""

from __future__ import annotations

from recovery.latency import PhaseStats, measure


def test_measure_runs_the_real_pipeline_and_times_both_phases():
    report = measure(cases=20)
    assert report.cases == 20
    assert len(report.ingest.samples_ms) == 20
    assert len(report.decision.samples_ms) == 20
    # Every sample is a real elapsed duration, not a placeholder.
    assert all(sample > 0 for sample in report.ingest.samples_ms)
    assert all(sample > 0 for sample in report.decision.samples_ms)


def test_decisions_are_not_trivially_free():
    """Guards against the measurement collapsing to a no-op -- e.g. if the
    decider were accidentally reused in a way that short-circuited real
    classification. A decision does real work: normalize, classify, allocate,
    guard, persist to SQLite -- it should reliably take at least on the order
    of a millisecond, not effectively zero."""
    report = measure(cases=20)
    assert report.decision.mean > 0.5


def test_cases_cycle_through_more_than_one_classifier_key():
    """The point of cycling _CASE_TEMPLATES: a latency figure measured against
    one repeated key would be measuring a warm code path, not a representative
    one. Confirmed indirectly -- if every case were identical the ingest
    samples would show implausibly tight clustering; instead just confirm the
    template list itself has real heterogeneity across rails and bands."""
    from recovery.latency import _CASE_TEMPLATES

    rails = {template[0] for template in _CASE_TEMPLATES}
    reasons = {template[3] for template in _CASE_TEMPLATES}
    assert len(rails) >= 3, "latency cases should span more than one rail"
    assert len(reasons) == len(_CASE_TEMPLATES), "each template should be a distinct key"


def test_phase_stats_percentiles_are_ordered():
    stats = PhaseStats(samples_ms=tuple(float(i) for i in range(1, 101)))
    assert stats.p50 <= stats.p90 <= stats.p99
    assert stats.mean > 0


def test_phase_stats_handles_small_samples_without_crashing():
    """statistics.quantiles needs enough points for its n; below that, p90/p99
    fall back to max() rather than raising -- a small --cases run must not
    crash the CLI."""
    stats = PhaseStats(samples_ms=(3.0, 5.0, 1.0))
    assert stats.p90 == 5.0
    assert stats.p99 == 5.0
    assert stats.p50 == 3.0
