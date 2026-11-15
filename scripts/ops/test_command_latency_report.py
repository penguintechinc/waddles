"""Unit tests for command_latency_report.py, driven by fixture Prometheus exposition text."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

import command_latency_report as rpt

FAST = """
# HELP waddles_command_e2e_seconds first door to outbound confirmed
# TYPE waddles_command_e2e_seconds histogram
waddles_command_e2e_seconds_bucket{command="ping",bundle_lang="rust",outcome="ok",door="gateway",otel_scope_name="command_latency",le="0.1"} 0
waddles_command_e2e_seconds_bucket{command="ping",bundle_lang="rust",outcome="ok",door="gateway",otel_scope_name="command_latency",le="0.5"} 90
waddles_command_e2e_seconds_bucket{command="ping",bundle_lang="rust",outcome="ok",door="gateway",otel_scope_name="command_latency",le="1"} 99
waddles_command_e2e_seconds_bucket{command="ping",bundle_lang="rust",outcome="ok",door="gateway",otel_scope_name="command_latency",le="+Inf"} 100
waddles_command_e2e_seconds_sum{command="ping",bundle_lang="rust",outcome="ok",door="gateway",otel_scope_name="command_latency"} 40
waddles_command_e2e_seconds_count{command="ping",bundle_lang="rust",outcome="ok",door="gateway",otel_scope_name="command_latency"} 100
waddles_command_hop_seconds_bucket{hop="process_stage",le="0.5"} 100
waddles_command_hop_seconds_bucket{hop="process_stage",le="+Inf"} 100
waddles_command_hop_seconds_count{hop="process_stage"} 100
waddles_command_hop_seconds_bucket{hop="outbound_call",le="0.1"} 100
waddles_command_hop_seconds_bucket{hop="outbound_call",le="+Inf"} 100
waddles_command_hop_seconds_count{hop="outbound_call"} 100
waddles_command_e2e_over_budget_total{command="ping",bundle_lang="rust",outcome="ok"} 1
"""

SLOW = """
waddles_command_e2e_seconds_bucket{command="roll",bundle_lang="python",outcome="ok",door="gateway",le="0.5"} 10
waddles_command_e2e_seconds_bucket{command="roll",bundle_lang="python",outcome="ok",door="gateway",le="1"} 50
waddles_command_e2e_seconds_bucket{command="roll",bundle_lang="python",outcome="ok",door="gateway",le="2.5"} 100
waddles_command_e2e_seconds_bucket{command="roll",bundle_lang="python",outcome="ok",door="gateway",le="+Inf"} 100
waddles_command_e2e_seconds_count{command="roll",bundle_lang="python",outcome="ok",door="gateway"} 100
waddles_command_hop_seconds_bucket{hop="executor_to_outbound",le="1"} 0
waddles_command_hop_seconds_bucket{hop="executor_to_outbound",le="2.5"} 100
waddles_command_hop_seconds_bucket{hop="executor_to_outbound",le="+Inf"} 100
waddles_command_hop_seconds_count{hop="executor_to_outbound"} 100
"""


def collect(*texts: str) -> rpt.Collection:
    """Build a Collection from exposition fixtures."""
    c = rpt.Collection()
    for t in texts:
        c.add(t)
    return c


def test_parse_labels_handles_escapes_and_commas() -> None:
    """Quoted values with escapes and commas parse into the right pairs."""
    got = rpt.parse_labels(r'a="x,y",b="q\"r",c=""')
    assert got == {"a": "x,y", "b": 'q"r', "c": ""}


def test_quantile_interpolates_inside_the_bucket_like_promql() -> None:
    """histogram_quantile semantics: linear interpolation within the containing bucket."""
    h = rpt.Histogram(count=100.0)
    h.buckets.update({0.5: 90.0, 1.0: 99.0, math.inf: 100.0})
    assert h.quantile(0.5) == pytest.approx(0.5 * (50 / 90))
    assert h.quantile(0.99) == pytest.approx(1.0)
    assert h.quantile(0.995) == math.inf


def test_quantile_none_without_samples() -> None:
    """An empty histogram has no quantile (never a made-up zero)."""
    assert rpt.Histogram().quantile(0.99) is None


def test_p99_exactly_at_the_budget_edge_is_over() -> None:
    """p99 at the 1 s edge is not >= budget only when strictly below; fixture p99 == 1.0 -> over."""
    text, code = rpt.build_report(collect(FAST))
    # 99/100 samples are <= 1 s, so p99 interpolates to exactly 1.0 -> NOT strictly under budget.
    assert code == rpt.EXIT_OVER
    assert "ping" in text and "OVER BUDGET" in text


def test_clearly_fast_command_passes() -> None:
    """A command whose p99 is well under 1 s passes with exit 0 and names the slowest hop."""
    fast = FAST.replace('le="1"} 99', 'le="1"} 100').replace(
        'le="0.5"} 90', 'le="0.5"} 100'
    )
    text, code = rpt.build_report(collect(fast))
    assert code == rpt.EXIT_OK, text
    assert "VERDICT: OK" in text
    assert "Slowest hop by p99: process_stage" in text


def test_slow_command_fails_and_slowest_hop_is_flagged() -> None:
    """A slow command is OVER BUDGET and the slowest hop by p99 is reported."""
    text, code = rpt.build_report(collect(FAST, SLOW))
    assert code == rpt.EXIT_OVER
    assert "roll" in text
    assert "Slowest hop by p99: executor_to_outbound" in text
    assert "p99 >= 1s for:" in text and "roll" in text.split("p99 >= 1s for:")[1]


def test_zero_samples_is_a_failure_not_a_pass() -> None:
    """No e2e samples examined -> EXIT_FAIL with the denominator in the message."""
    text, code = rpt.build_report(collect("some_other_metric 1\n"))
    assert code == rpt.EXIT_FAIL
    assert "zero command samples examined" in text
    assert "1 metric lines read" in text


def test_series_from_two_pods_are_summed() -> None:
    """The same series from two sources adds up (one scrape per pod)."""
    c = collect(FAST, FAST)
    groups = c.grouped(rpt.E2E, "command")
    assert groups["ping"].count == 200
    assert groups["ping"].buckets[0.5] == 180


def test_over_budget_counter_is_attached_to_the_row() -> None:
    """The over-budget counter column is read from the *_total counter."""
    c = collect(FAST)
    rows = rpt.rows_for(
        c.grouped(rpt.E2E, "command"), c.counter_by(rpt.OVER_BUDGET, "command")
    )
    assert rows[0].over_budget == 1


def test_main_reads_a_file_and_exit_codes_propagate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() over --file prints the report and returns the verdict's exit code."""
    f = tmp_path / "m.txt"
    f.write_text(SLOW)
    assert rpt.main(["--file", str(f)]) == rpt.EXIT_OVER
    assert "roll" in capsys.readouterr().out
    empty = tmp_path / "e.txt"
    empty.write_text("# nothing\n")
    assert rpt.main(["--file", str(empty)]) == rpt.EXIT_FAIL


def test_unreadable_sources_fail_loud(capsys: pytest.CaptureFixture[str]) -> None:
    """A missing file and a bad URL scheme leave no readable source -> EXIT_FAIL."""
    assert (
        rpt.main(
            ["--file", "/nonexistent/metrics.txt", "--url", "ftp://example.invalid/m"]
        )
        == rpt.EXIT_FAIL
    )
    err = capsys.readouterr().err
    assert "FAIL: no metrics source could be read" in err


def test_fetch_refuses_non_http_schemes() -> None:
    """The fetcher only ever issues http(s) GETs."""
    with pytest.raises(ValueError):
        rpt.fetch("file:///etc/passwd", 1.0)
