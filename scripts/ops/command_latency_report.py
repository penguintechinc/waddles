#!/usr/bin/env python3
r"""Read-only command-latency report: p50/p95/p99 per command and per hop.

Owner SLA (2026-11-14): non-external bot commands must have p99 < 1 s, first door ->
outbound platform call completed. The data-plane services publish that as the histograms
``waddles_command_e2e_seconds{command,bundle_lang,outcome,door}`` and
``waddles_command_hop_seconds{hop}`` (see docs/observability/data-plane-metrics.md).

This script only performs HTTP GETs of Prometheus text exposition (a port-forwarded service
``/metrics``, or a saved file) and prints a table. It never writes, never mutates a cluster,
and never sends anything but a plain GET. Samples from several sources (one per pod) are summed.

    kubectl --context dal2alpha -n waddlebot port-forward svc/waddlebot-svc-action-rust 19090:9090 &
    kubectl --context dal2alpha -n waddlebot port-forward svc/waddlebot-svc-process-rust 19091:9090 &
    scripts/ops/command_latency_report.py --url http://127.0.0.1:19090/metrics \\
        --url http://127.0.0.1:19091/metrics

Exit status: 0 = every command with samples has p99 < 1 s; 1 = at least one command is over
budget; 2 = FAIL, no samples (zero examined is a failure, never a pass) or no source readable.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlparse

BUDGET_S = 1.0
E2E = "waddles_command_e2e_seconds"
HOP = "waddles_command_hop_seconds"
CANARY = "waddles_canary_e2e_seconds"
OVER_BUDGET = "waddles_command_e2e_over_budget_total"
QUANTILES = (0.5, 0.95, 0.99)

EXIT_OK = 0
EXIT_OVER = 1
EXIT_FAIL = 2

_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>.*)\})?"
    r"\s+(?P<value>\S+)(?:\s+-?\d+)?\s*$"
)
_LABEL = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*(?:,|$)')

Labels = tuple[tuple[str, str], ...]


@dataclass
class Histogram:
    """Cumulative bucket counts (``le`` upper bound -> count) summed across sources."""

    buckets: dict[float, float] = field(default_factory=lambda: defaultdict(float))
    count: float = 0.0
    total: float = 0.0

    def quantile(self, q: float) -> float | None:
        """Prometheus ``histogram_quantile``: linear interpolation inside the bucket.

        ``None`` when there are no samples. ``inf`` when the quantile lands in the +Inf bucket
        (the true value is above the largest finite bucket).
        """
        if self.count <= 0 or not self.buckets:
            return None
        bounds = sorted(self.buckets)
        rank = (
            q * self.buckets[bounds[-1]] if math.isinf(bounds[-1]) else q * self.count
        )
        lower, lower_count = 0.0, 0.0
        for bound in bounds:
            cum = self.buckets[bound]
            if cum >= rank:
                if math.isinf(bound):
                    return math.inf
                span = cum - lower_count
                if span <= 0:
                    return bound
                return lower + (bound - lower) * ((rank - lower_count) / span)
            lower, lower_count = bound, cum
        return math.inf


def parse_labels(raw: str) -> dict[str, str]:
    """Parse the inside of ``{...}`` of a sample line (quoted, escaped values)."""
    out: dict[str, str] = {}
    pos = 0
    raw = raw.strip()
    while pos < len(raw):
        m = _LABEL.match(raw, pos)
        if not m:
            break
        out[m.group(1)] = m.group(2).replace('\\"', '"').replace("\\\\", "\\")
        pos = m.end()
    return out


def parse_exposition(text: str) -> Iterable[tuple[str, dict[str, str], float]]:
    """Yield ``(name, labels, value)`` for each sample line; comments and junk are skipped."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE.match(line)
        if not m:
            continue
        try:
            value = float(m.group("value"))
        except ValueError:
            continue
        yield m.group("name"), parse_labels(m.group("labels") or ""), value


def _le(raw: str) -> float:
    return math.inf if raw in ("+Inf", "Inf", "inf") else float(raw)


class Collection:
    """Histograms and counters from every source, keyed by metric family and label set."""

    def __init__(self) -> None:
        """Create an empty collection."""
        self.hist: dict[str, dict[Labels, Histogram]] = defaultdict(dict)
        self.counters: dict[str, dict[Labels, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        self.samples_read = 0

    def add(self, text: str) -> None:
        """Fold one exposition body (one pod's scrape) into the collection."""
        for name, labels, value in parse_exposition(text):
            self.samples_read += 1
            if name.endswith("_bucket") and "le" in labels:
                family = name[: -len("_bucket")]
                key = _key(labels)
                h = self.hist[family].setdefault(key, Histogram())
                h.buckets[_le(labels["le"])] += value
            elif name.endswith("_count"):
                family = name[: -len("_count")]
                self.hist[family].setdefault(_key(labels), Histogram()).count += value
            elif name.endswith("_sum"):
                family = name[: -len("_sum")]
                self.hist[family].setdefault(_key(labels), Histogram()).total += value
            elif name.endswith("_total"):
                self.counters[name][_key(labels)] += value

    def grouped(self, family: str, by: str) -> dict[str, Histogram]:
        """Sum the family's series into one histogram per value of label ``by``."""
        out: dict[str, Histogram] = {}
        for key, h in self.hist.get(family, {}).items():
            group = dict(key).get(by, "")
            agg = out.setdefault(group, Histogram())
            agg.count += h.count
            agg.total += h.total
            for bound, n in h.buckets.items():
                agg.buckets[bound] += n
        return out

    def counter_by(self, name: str, by: str) -> dict[str, float]:
        """Sum a counter family per value of label ``by``."""
        out: dict[str, float] = defaultdict(float)
        for key, v in self.counters.get(name, {}).items():
            out[dict(key).get(by, "")] += v
        return out


def _key(labels: dict[str, str]) -> Labels:
    """Series identity: every label except ``le`` and the per-pod scrape/scope noise."""
    drop = {
        "le",
        "otel_scope_name",
        "otel_scope_version",
        "otel_scope_schema_url",
        "instance",
        "pod",
        "job",
    }
    return tuple(sorted((k, v) for k, v in labels.items() if k not in drop))


def fmt_ms(seconds: float | None) -> str:
    """Format seconds as whole milliseconds (``-`` none, ``>max`` beyond the top bucket)."""
    if seconds is None:
        return "-"
    if math.isinf(seconds):
        return ">max"
    return f"{seconds * 1000:.0f}ms"


@dataclass
class Row:
    """One report line: a command or hop with its sample count and quantiles."""

    name: str
    samples: int
    p50: float | None
    p95: float | None
    p99: float | None
    over_budget: int | None = None

    @property
    def within_budget(self) -> bool:
        """Whether p99 is strictly under the 1 s budget."""
        return self.p99 is not None and self.p99 < BUDGET_S


def rows_for(
    groups: dict[str, Histogram], over: dict[str, float] | None = None
) -> list[Row]:
    """Turn grouped histograms into sorted report rows."""
    rows = []
    for name, h in sorted(groups.items()):
        rows.append(
            Row(
                name=name or "(none)",
                samples=int(h.count),
                p50=h.quantile(QUANTILES[0]),
                p95=h.quantile(QUANTILES[1]),
                p99=h.quantile(QUANTILES[2]),
                over_budget=int(over.get(name, 0)) if over is not None else None,
            )
        )
    return rows


def render(title: str, rows: Sequence[Row], flag_budget: bool) -> str:
    """Render one titled table; with ``flag_budget`` each row gets a p99 verdict."""
    lines = [
        title,
        f"  {'name':<28}{'samples':>9}{'p50':>9}{'p95':>9}{'p99':>9}  {'>1s':>6}  verdict",
    ]
    for r in rows:
        verdict = ""
        if flag_budget:
            verdict = "OK p99<1s" if r.within_budget else "OVER BUDGET (p99>=1s)"
        over = "" if r.over_budget is None else str(r.over_budget)
        lines.append(
            f"  {r.name:<28}{r.samples:>9}{fmt_ms(r.p50):>9}{fmt_ms(r.p95):>9}{fmt_ms(r.p99):>9}  {over:>6}  {verdict}"
        )
    return "\n".join(lines)


def slowest_hop(rows: Sequence[Row]) -> Row | None:
    """Return the row with the largest p99, or ``None`` when no row has one."""
    best: Row | None = None
    best_p99 = -1.0
    for row in rows:
        if row.p99 is not None and row.p99 > best_p99:
            best, best_p99 = row, row.p99
    return best


def fetch(url: str, timeout: float) -> str:
    """Plain GET of a metrics URL (http/https only)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")
    req = urllib.request.Request(url, method="GET", headers={"Accept": "text/plain"})  # noqa: S310 - scheme checked
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme checked
        body: bytes = resp.read()
    return body.decode("utf-8", errors="replace")


def build_report(collection: Collection) -> tuple[str, int]:
    """Render the whole report and return it with the exit status."""
    e2e = rows_for(
        collection.grouped(E2E, "command"),
        collection.counter_by(OVER_BUDGET, "command"),
    )
    hops = rows_for(collection.grouped(HOP, "hop"))
    canary = rows_for(collection.grouped(CANARY, "outcome"))
    out: list[str] = []
    examined = sum(r.samples for r in e2e)
    if examined == 0:
        out.append(
            "FAIL: zero command samples examined "
            f"({collection.samples_read} metric lines read, none in {E2E}); "
            "a report that examined nothing is not a pass."
        )
        if hops:
            out.append(
                render("Per-hop (no end-to-end samples yet):", hops, flag_budget=False)
            )
        return "\n".join(out), EXIT_FAIL
    out.append(
        render(
            f"End-to-end per command ({examined} samples examined):",
            e2e,
            flag_budget=True,
        )
    )
    if hops:
        out.append(
            render("\nPer hop (p99 attributes the time):", hops, flag_budget=False)
        )
        slow = slowest_hop(hops)
        if slow is not None:
            out.append(f"\nSlowest hop by p99: {slow.name} ({fmt_ms(slow.p99)})")
    else:
        out.append(
            "\nPer hop: no samples (hop histograms absent from the sources given)."
        )
    if canary:
        out.append(render("\nSynthetic canary:", canary, flag_budget=True))
    over = [r for r in e2e if not r.within_budget]
    if over:
        names = ", ".join(r.name for r in over)
        out.append(f"\nVERDICT: OVER BUDGET -- p99 >= 1s for: {names}")
        return "\n".join(out), EXIT_OVER
    out.append("\nVERDICT: OK -- every command's p99 < 1s")
    return "\n".join(out), EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: read the sources, print the report, return the exit status."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--url",
        action="append",
        default=[],
        help="a /metrics URL (repeatable; one per pod/service)",
    )
    ap.add_argument(
        "--file",
        action="append",
        default=[],
        help="a saved exposition file (repeatable)",
    )
    ap.add_argument(
        "--timeout", type=float, default=10.0, help="per-URL timeout, seconds"
    )
    args = ap.parse_args(argv)
    if not args.url and not args.file:
        ap.error("give at least one --url or --file")
    collection = Collection()
    readable = 0
    for url in args.url:
        try:
            collection.add(fetch(url, args.timeout))
            readable += 1
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(
                f"WARN: could not read {urlparse(url).netloc}: {exc}", file=sys.stderr
            )
    for path in args.file:
        try:
            with open(path, encoding="utf-8") as fh:
                collection.add(fh.read())
            readable += 1
        except OSError as exc:
            print(f"WARN: could not read {path}: {exc}", file=sys.stderr)
    if readable == 0:
        print("FAIL: no metrics source could be read", file=sys.stderr)
        return EXIT_FAIL
    text, code = build_report(collection)
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
