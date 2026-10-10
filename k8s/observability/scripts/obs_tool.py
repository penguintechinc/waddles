#!/usr/bin/env python3
"""Observability-as-code tool for Waddles: dashboards, rule manifests, metric checks.

Generates the Grafana dashboard JSON in ``../dashboards`` from the Python
definitions below, renders the PrometheusRule manifest from the plain Prometheus
rule file, and verifies that every metric a dashboard or alert references is a
metric the code base really emits (or an explicitly allowlisted external one).
Run through ``scripts/validate.sh`` / ``make -C k8s/observability``; stdlib +
PyYAML only.

Subcommands:
  dashboards [--out DIR]       write dashboard JSON (default: ../dashboards)
  prometheusrule [--out FILE]  render ../manifests/prometheusrule.yaml
  promql-rules --out FILE      wrap every dashboard query in a rule file for promtool
  check-metrics                fail on any referenced metric the code does not emit
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent  # k8s/observability
REPO = ROOT.parent.parent  # repository root
DASH_DIR = ROOT / "dashboards"
RULES_FILE = ROOT / "alerts" / "waddles-slo.rules.yml"
RULE_MANIFEST = ROOT / "manifests" / "prometheusrule.yaml"

DS = {"type": "prometheus", "uid": "${datasource}"}
RATE = "$rate"  # dashboard variable, see _templating(); OTLP exports every 60s so >= 2m


def sel(name: str, *matchers: str) -> str:
    """Builds a selector ``name{m1,m2}`` from raw label-matcher strings."""
    return name + ("{" + ",".join(matchers) + "}" if matchers else "")


def rate(name: str, *matchers: str) -> str:
    """``rate(<selector>[$rate])``."""
    return f"rate({sel(name, *matchers)}[{RATE}])"


def sumrate(name: str, *matchers: str, by: str | None = None) -> str:
    """``sum [by (..)] (rate(..))``."""
    inner = rate(name, *matchers)
    return f"sum by ({by}) ({inner})" if by else f"sum({inner})"


def increase(name: str, *matchers: str) -> str:
    """``increase(<selector>[$rate])``."""
    return f"increase({sel(name, *matchers)}[{RATE}])"


def ratio(num: str, den: str) -> str:
    """Division of two expressions (NaN when the denominator is zero)."""
    return f"{num} / {den}"


def hq(q: float, hist: str, *matchers: str, by: str | None = None) -> str:
    """``histogram_quantile`` over ``<hist>_bucket`` aggregated by ``le`` (+ ``by``)."""
    grp = f"le, {by}" if by else "le"
    return f"histogram_quantile({q}, sum by ({grp}) (rate({sel(hist + '_bucket', *matchers)}[{RATE}])))"


def quantiles(
    hist: str, *matchers: str, by: str | None = None
) -> list[tuple[str, str]]:
    """p50/p95/p99 targets for one histogram."""
    suffix = f" {{{{{by}}}}}" if by else ""
    return [
        (hq(q, hist, *matchers, by=by), f"p{int(q * 100)}{suffix}")
        for q in (0.5, 0.95, 0.99)
    ]


def slow_share(hist: str, le: str, *matchers: str) -> str:
    """Share of observations above bucket ``le`` -- robust to coarse bucket layouts."""
    fast = sel(hist + "_bucket", *matchers, 'le="' + le + '"')
    total = sel(hist + "_count", *matchers)
    return f"1 - sum(rate({fast}[{RATE}])) / sum(rate({total}[{RATE}]))"


def name_re(base: str, unit_suffix: str, tail: str) -> str:
    """Selector matching a metric with or without the Prometheus unit suffix."""
    return f'{{__name__=~"{base}({unit_suffix})?{tail}"}}'


class Board:
    """Accumulates panels on a 24-column grid and renders Grafana dashboard JSON."""

    def __init__(self, uid: str, title: str, description: str) -> None:
        self.uid, self.title, self.description = uid, title, description
        self.panels: list[dict] = []
        self._id = 0
        self._x = 0
        self._y = 0
        self._row_h = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _place(self, w: int, h: int) -> dict:
        if self._x + w > 24:
            self._x, self._y, self._row_h = 0, self._y + self._row_h, 0
        pos = {"h": h, "w": w, "x": self._x, "y": self._y}
        self._x += w
        self._row_h = max(self._row_h, h)
        return pos

    def row(self, title: str) -> None:
        """Starts a new (expanded) row; later panels flow beneath it."""
        self._y += self._row_h
        self._x, self._row_h = 0, 0
        self.panels.append(
            {
                "id": self._next_id(),
                "type": "row",
                "title": title,
                "collapsed": False,
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": self._y},
                "panels": [],
            }
        )
        self._y += 1

    @staticmethod
    def _targets(targets: list[tuple[str, str]]) -> list[dict]:
        return [
            {
                "datasource": DS,
                "expr": expr,
                "legendFormat": legend,
                "refId": chr(ord("A") + i),
                "range": True,
            }
            for i, (expr, legend) in enumerate(targets)
        ]

    def stat(
        self,
        title: str,
        expr: str,
        unit: str = "short",
        desc: str = "",
        thresholds: list[tuple[float | None, str]] | None = None,
        w: int = 6,
        h: int = 4,
        legend: str = "",
    ) -> None:
        """Single-value tile with optional colour thresholds."""
        steps = [{"color": c, "value": v} for v, c in (thresholds or [(None, "green")])]
        self.panels.append(
            {
                "id": self._next_id(),
                "type": "stat",
                "title": title,
                "description": desc,
                "datasource": DS,
                "gridPos": self._place(w, h),
                "targets": self._targets([(expr, legend)]),
                "fieldConfig": {
                    "defaults": {
                        "unit": unit,
                        "thresholds": {"mode": "absolute", "steps": steps},
                        "color": {"mode": "thresholds"},
                    },
                    "overrides": [],
                },
                "options": {
                    "reduceOptions": {
                        "calcs": ["lastNotNull"],
                        "fields": "",
                        "values": False,
                    },
                    "colorMode": "background",
                    "graphMode": "area",
                    "textMode": "auto",
                },
            }
        )

    def ts(
        self,
        title: str,
        targets: list[tuple[str, str]],
        unit: str = "short",
        desc: str = "",
        w: int = 12,
        h: int = 8,
        stack: bool = False,
        thresholds: list[tuple[float | None, str]] | None = None,
    ) -> None:
        """Time-series panel (stacked bars of area when ``stack``)."""
        defaults: dict = {
            "unit": unit,
            "custom": {
                "drawStyle": "line",
                "lineWidth": 1,
                "fillOpacity": 18 if stack else 6,
                "showPoints": "never",
                "spanNulls": False,
                "stacking": {"mode": "normal" if stack else "none", "group": "A"},
            },
        }
        if thresholds:
            defaults["thresholds"] = {
                "mode": "absolute",
                "steps": [{"color": c, "value": v} for v, c in thresholds],
            }
            defaults["custom"]["thresholdsStyle"] = {"mode": "line"}
        self.panels.append(
            {
                "id": self._next_id(),
                "type": "timeseries",
                "title": title,
                "description": desc,
                "datasource": DS,
                "gridPos": self._place(w, h),
                "targets": self._targets(targets),
                "fieldConfig": {"defaults": defaults, "overrides": []},
                "options": {
                    "legend": {
                        "displayMode": "table",
                        "placement": "bottom",
                        "calcs": ["lastNotNull", "max"],
                    },
                    "tooltip": {"mode": "multi", "sort": "desc"},
                },
            }
        )

    def http_red(self, svc: str, prefix: str) -> None:
        """RED row for one service's ``<prefix>_http_*`` request metrics."""
        req, dur = (
            f"{prefix}_http_requests_total",
            f"{prefix}_http_request_duration_seconds",
        )
        self.row(f"{svc} - HTTP RED")
        self.stat(
            f"{svc} 5xx ratio",
            ratio(sumrate(req, 'status=~"5.."'), sumrate(req)),
            "percentunit",
            "Share of responses with a 5xx status.",
            [(None, "green"), (0.01, "yellow"), (0.05, "red")],
        )
        self.stat(
            f"{svc} p99 latency",
            hq(0.99, dur),
            "s",
            "Request duration p99, all routes.",
            [(None, "green"), (0.5, "yellow"), (1, "red")],
        )
        self.ts(
            f"{svc} throughput by status",
            [(sumrate(req, by="status"), "{{status}}")],
            "reqps",
            stack=True,
            w=6,
        )
        self.ts(f"{svc} latency p50/p95/p99", quantiles(dur), "s", w=6)

    def render(self) -> dict:
        """The complete Grafana dashboard document."""
        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.description,
            "tags": ["waddles", "observability-as-code"],
            "schemaVersion": 39,
            "version": 1,
            "editable": True,
            "graphTooltip": 1,
            "timezone": "browser",
            "refresh": "30s",
            "time": {"from": "now-6h", "to": "now"},
            "links": [],
            "annotations": {"list": []},
            "templating": _templating(),
            "panels": self.panels,
        }


def _templating() -> dict:
    return {
        "list": [
            {
                "type": "datasource",
                "name": "datasource",
                "label": "Prometheus",
                "query": "prometheus",
                "current": {},
                "hide": 0,
                "includeAll": False,
                "multi": False,
                "refresh": 1,
                "regex": "",
            },
            {
                "type": "custom",
                "name": "rate",
                "label": "Rate window",
                "query": "2m,5m,10m,30m",
                "current": {"selected": True, "text": "5m", "value": "5m"},
                "options": [
                    {"selected": v == "5m", "text": v, "value": v}
                    for v in ("2m", "5m", "10m", "30m")
                ],
                "hide": 0,
                "includeAll": False,
                "multi": False,
            },
            {
                "type": "adhoc",
                "name": "filters",
                "label": "Filters (namespace, job, ...)",
                "datasource": DS,
                "filters": [],
                "hide": 0,
            },
        ]
    }


BUCKET_NOTE = (
    " The OTel SDK default bucket layout is millisecond-oriented, so quantiles of a seconds histogram "
    "without a View are coarse (everything under 5 s lands in one bucket); the slow-call share is the "
    "reliable signal."
)


def hub_api_board() -> Board:
    """hub-api: internal gRPC RED plus identity, flags, watermark and sync-job metrics."""
    b = Board(
        "waddles-hub-api",
        "Waddles / hub-api",
        "hub-api (Python/Quart): internal gRPC RED, identity resolution, flags, active-set watermark, usage "
        "aggregation and sync jobs. All series are OTel instruments pushed over OTLP.",
    )
    g = "grpc_server_request_duration_seconds"
    srv_err = 'rpc_grpc_status_code=~"INTERNAL|UNKNOWN|UNAVAILABLE|DEADLINE_EXCEEDED|DATA_LOSS"'
    b.row("Internal gRPC (data plane -> hub-api) - RED")
    b.stat(
        "Request rate",
        sumrate(g + "_count"),
        "reqps",
        "Internal gRPC requests per second (OTel histogram count).",
    )
    b.stat(
        "Server error ratio",
        ratio(sumrate(g + "_count", srv_err), sumrate(g + "_count")),
        "percentunit",
        "INTERNAL / UNKNOWN / UNAVAILABLE / DEADLINE_EXCEEDED / DATA_LOSS over all RPCs. Client-class codes "
        "(INVALID_ARGUMENT, PERMISSION_DENIED, ...) are excluded.",
        [(None, "green"), (0.01, "yellow"), (0.05, "red")],
    )
    b.stat(
        "p99 latency",
        hq(0.99, g),
        "s",
        "RPC latency p99." + BUCKET_NOTE,
        [(None, "green"), (2.5, "yellow"), (5, "red")],
    )
    b.stat(
        "Calls slower than 5 s",
        slow_share(g, "5"),
        "percentunit",
        "Share of RPCs slower than 5 s." + BUCKET_NOTE,
        [(None, "green"), (0.005, "yellow"), (0.01, "red")],
    )
    b.ts(
        "Throughput by method",
        [(sumrate(g + "_count", by="rpc_method"), "{{rpc_method}}")],
        "reqps",
        stack=True,
    )
    b.ts("Latency p50/p95/p99", quantiles(g), "s", BUCKET_NOTE.strip())
    b.ts(
        "Non-OK responses by status code",
        [
            (
                sumrate(
                    g + "_count",
                    'rpc_grpc_status_code!="OK"',
                    by="rpc_grpc_status_code",
                ),
                "{{rpc_grpc_status_code}}",
            )
        ],
        "reqps",
        "Everything except OK, including client-class codes.",
        stack=True,
    )
    b.ts(
        "p95 latency by method", [(hq(0.95, g, by="rpc_method"), "{{rpc_method}}")], "s"
    )

    b.row("Identity resolution")
    ident = "hub_api_identity"
    b.ts(
        "Resolutions by outcome",
        [(sumrate(f"{ident}_resolutions_total", by="outcome"), "{{outcome}}")],
        "ops",
        stack=True,
    )
    b.ts(
        "Operations by op / outcome",
        [
            (
                sumrate(f"{ident}_operations_total", by="op, outcome"),
                "{{op}} {{outcome}}",
            )
        ],
        "ops",
        stack=True,
    )
    resolve = name_re(f"{ident}_resolve_duration_ms", "_milliseconds", "_bucket")
    b.ts(
        "Batch resolve latency p50/p95/p99",
        [
            (
                f"histogram_quantile({q}, sum by (le) (rate({resolve}[{RATE}])))",
                f"p{int(q * 100)}",
            )
            for q in (0.5, 0.95, 0.99)
        ],
        "ms",
        "OTel unit is ms; the Prometheus name carries a unit suffix only on some translation strategies, so the "
        "selector matches both spellings.",
    )
    opdur = name_re(f"{ident}_operation_duration_ms", "_milliseconds", "_bucket")
    b.ts(
        "Lookup latency p95 by op",
        [
            (
                f"histogram_quantile(0.95, sum by (le, op) (rate({opdur}[{RATE}])))",
                "{{op}}",
            )
        ],
        "ms",
    )
    b.ts(
        "Display-name lookups hit / miss",
        [(sumrate(f"{ident}_display_name_lookups_total", by="result"), "{{result}}")],
        "ops",
        stack=True,
    )
    b.ts(
        "Batch size p95",
        [(hq(0.95, f"{ident}_batch_size"), "p95 items / batch")],
        "short",
    )

    b.row("Flags and component onboarding")
    b.ts(
        "Flag resolution requests",
        [
            (
                sumrate("waddles_hub_api_flag_resolution_requests_total"),
                "GET /api/v1/flags",
            )
        ],
        "reqps",
    )
    b.ts(
        "Component onboarding by outcome",
        [
            (
                sumrate("waddles_hub_component_onboarding_total", by="outcome"),
                "{{outcome}}",
            )
        ],
        "ops",
        stack=True,
    )

    b.row("Active-set watermark and usage aggregation")
    comp = "waddles_bundle_active_set_watermark_computed_at_epoch_seconds"
    b.stat(
        "Watermark age",
        f"time() - max({comp})",
        "s",
        "Seconds since the last successful safe_seq computation. The job's own alert threshold is "
        "SAFE_SEQ_STALL_ALERT (default 30 s) plus up to one OTLP export interval.",
        [(None, "green"), (60, "yellow"), (120, "red")],
    )
    b.ts("safe_seq", [("max(waddles_bundle_active_set_safe_seq)", "safe_seq")], "short")
    b.ts(
        "Watermark tick failures / replica skips",
        [
            (
                increase("waddles_bundle_active_set_watermark_tick_failures_total"),
                "tick failures",
            ),
            (
                increase("waddles_bundle_active_set_watermark_replica_skipped_total"),
                "skipped (standby DB)",
            ),
        ],
        "short",
        w=6,
    )
    b.ts(
        "Change rows pruned (p95 per pass)",
        [(hq(0.95, "waddles_bundle_active_set_changes_pruned"), "p95")],
        "short",
        w=6,
    )
    b.ts(
        "Usage batches by result",
        [(sumrate("waddles_hub_usage_batches_total", by="result"), "{{result}}")],
        "ops",
        stack=True,
    )
    b.ts(
        "Usage entries skipped by reason",
        [
            (
                sumrate("waddles_hub_usage_entries_skipped_total", by="reason"),
                "{{reason}}",
            )
        ],
        "ops",
        "Entries acked without a workstream_usage_hourly write (unparseable or poison insert).",
        stack=True,
    )
    b.ts(
        "Usage rows written per batch (p95)",
        [(hq(0.95, "waddles_hub_usage_rows_written"), "p95")],
        "short",
    )

    b.row("Integrations - role sync, event sync, Odoo")
    b.ts(
        "Role sync changes",
        [
            (sumrate("waddles_bar_citizen_roles_added_total"), "roles added"),
            (sumrate("waddles_bar_citizen_roles_removed_total"), "roles removed"),
            (
                sumrate("waddles_bar_citizen_community_roles_applied_total"),
                "community roles applied",
            ),
        ],
        "ops",
    )
    b.ts(
        "Role sync errors",
        [(sumrate("waddles_bar_citizen_sync_errors_total"), "pairing failures")],
        "ops",
        "Fail-closed pairing failures.",
    )
    b.ts(
        "Event sync to Discord",
        [
            (sumrate("waddles_event_sync_synced_total"), "synced"),
            (sumrate("waddles_event_sync_errors_total"), "errors"),
            (sumrate("waddles_event_sync_drift_total"), "drift detected"),
        ],
        "ops",
    )
    b.ts(
        "Odoo sync records",
        [
            (sumrate("odoo_sync_records_created_total"), "created"),
            (sumrate("odoo_sync_records_updated_total"), "updated"),
            (sumrate("odoo_sync_errors_total"), "errors"),
            (sumrate("odoo_sync_rpc_retries_total"), "rpc retries"),
        ],
        "ops",
    )
    rpc = name_re("odoo_sync_rpc_duration_ms", "_milliseconds", "_bucket")
    b.ts(
        "Odoo RPC latency p50/p95/p99",
        [
            (
                f"histogram_quantile({q}, sum by (le) (rate({rpc}[{RATE}])))",
                f"p{int(q * 100)}",
            )
            for q in (0.5, 0.95, 0.99)
        ],
        "ms",
        w=24,
    )
    return b


def data_plane_board() -> Board:
    """svc_ingest / svc_process / svc_action: event flow, backlog proxies, failures, HTTP RED."""
    b = Board(
        "waddles-data-plane",
        "Waddles / data plane (ingest, process, action)",
        "Rust data-plane stages: svc-ingest, svc-process, svc-action and the egress proxy. Scraped from /metrics "
        "(:9090); svc-ingest EventSub latency is the only per-event latency histogram emitted today.",
    )
    b.row("Pipeline overview")
    b.stat(
        "Stages up",
        "(sum(svc_ingest_up) or vector(0)) + (sum(svc_process_up) or vector(0)) + (sum(svc_action_up) or vector(0))",
        "short",
        "Running replicas reporting an *_up gauge (ingest + process + action).",
        [(None, "red"), (3, "green")],
    )
    b.stat(
        "Events published / s",
        sumrate("svc_ingest_events_published_total"),
        "ops",
        "Normalized platform events XADDed onto the spine.",
    )
    b.stat(
        "Publish error ratio",
        ratio(
            sumrate("svc_ingest_publish_errors_total"),
            f"({sumrate('svc_ingest_events_published_total')} + {sumrate('svc_ingest_publish_errors_total')})",
        ),
        "percentunit",
        "svc_ingest_publish_errors_total over attempted publishes.",
        [(None, "green"), (0.01, "yellow"), (0.02, "red")],
    )
    b.stat(
        "Consumer loops down",
        "(count(svc_ingest_consumer_loop_running == 0) or vector(0)) + (count(svc_process_consumer_loop_running == 0) or vector(0)) + (count(svc_action_consumer_loop_running == 0) or vector(0))",
        "short",
        "Spine consumer/receiver loops currently disconnected or retrying (0 is healthy).",
        [(None, "green"), (1, "red")],
    )
    b.stat(
        "Executors connected",
        "sum by (job) (host_api_connected_executors)",
        "short",
        "Bundle-executor sessions held by each stage (svc-process / svc-action).",
        [(None, "red"), (1, "green")],
        legend="{{job}}",
    )
    b.stat(
        "Dead-lettered (no executor), 1h",
        "sum(increase(dispatch_dead_lettered_no_executor_total[1h]))",
        "short",
        "Dispatch entries dead-lettered because no executor connection existed.",
        [(None, "green"), (1, "red")],
    )

    b.row("svc-ingest - event ingest")
    b.ts(
        "Events published by platform",
        [(sumrate("svc_ingest_events_published_total", by="platform"), "{{platform}}")],
        "ops",
        stack=True,
    )
    b.ts(
        "Publish errors by platform / reason",
        [
            (
                sumrate("svc_ingest_publish_errors_total", by="platform, reason"),
                "{{platform}} {{reason}}",
            )
        ],
        "ops",
        stack=True,
    )
    b.ts(
        "Receiver reconnects by platform / reason",
        [
            (
                sumrate("svc_ingest_receiver_reconnects_total", by="platform, reason"),
                "{{platform}} {{reason}}",
            )
        ],
        "ops",
    )
    b.ts(
        "Receiver connection healthy",
        [
            (
                "min by (platform) (svc_ingest_receiver_connection_healthy)",
                "{{platform}}",
            )
        ],
        "short",
        "1 healthy, 0 = fatal non-retryable close stopped that platform's reconnect loop (process keeps running).",
        thresholds=[(None, "red"), (1, "green")],
    )
    b.ts(
        "EventSub verification outcomes",
        [
            (
                sumrate("svc_ingest_eventsub_verifications_total", by="outcome"),
                "{{outcome}}",
            )
        ],
        "ops",
        "ok vs bad_signature / replay_rejected / secret_not_found / missing_header / bad_content_type / oversized_body.",
        stack=True,
    )
    b.ts(
        "EventSub handler latency p50/p95/p99",
        quantiles("svc_ingest_eventsub_request_duration_seconds"),
        "s",
        "Webhook fast-path latency (Twitch requires an answer within 10 s).",
    )
    b.ts(
        "EventSub duplicate deliveries",
        [(sumrate("svc_ingest_eventsub_dedup_hits_total"), "dedup hits")],
        "ops",
    )
    b.ts(
        "Spine connect attempts by receiver",
        [
            (
                sumrate("svc_ingest_spine_connect_attempts_total", by="receiver"),
                "{{receiver}}",
            )
        ],
        "ops",
        "Every connect-retry increments; a sustained non-zero rate means a receiver is stuck retrying.",
    )
    b.ts(
        "Receiver loop running",
        [("min by (receiver) (svc_ingest_consumer_loop_running)", "{{receiver}}")],
        "short",
        thresholds=[(None, "red"), (1, "green")],
    )

    b.row("svc-process / svc-action - stage workers")
    b.ts(
        "Drain / consumer loops running",
        [
            ("min by (loop) (svc_process_consumer_loop_running)", "process {{loop}}"),
            ("min by (loop) (svc_action_consumer_loop_running)", "action {{loop}}"),
        ],
        "short",
        thresholds=[(None, "red"), (1, "green")],
    )
    b.ts(
        "Spine connect attempts by loop",
        [
            (
                sumrate("svc_process_spine_connect_attempts_total", by="loop"),
                "process {{loop}}",
            ),
            (
                sumrate("svc_action_spine_connect_attempts_total", by="loop"),
                "action {{loop}}",
            ),
        ],
        "ops",
    )
    b.ts(
        "Consumer tasks running",
        [
            (
                "sum(svc_process_source_binding_consumers_active)",
                "process source-binding consumers",
            ),
            ("sum(svc_action_dispatch_consumers_running)", "action dispatch consumers"),
        ],
        "short",
    )
    b.ts(
        "Consumer spawn / stop transitions",
        [
            (
                sumrate(
                    "svc_process_source_binding_consumer_transitions_total", by="action"
                ),
                "process {{action}}",
            ),
            (
                sumrate("svc_action_dispatch_consumer_transitions_total", by="action"),
                "action {{action}}",
            ),
        ],
        "ops",
        "A flapping binding shows up as continuous spawn/stop churn.",
    )
    b.ts(
        "Consumer groups created",
        [
            (increase("svc_process_consumer_group_created_total"), "process {{loop}}"),
            (increase("svc_action_consumer_group_created_total"), "action {{loop}}"),
        ],
        "short",
        "XGROUP CREATE (not BUSYGROUP) by the self-heal / startup provisioning step.",
    )
    b.ts(
        "Tenant active apps (top 10)",
        [("topk(10, svc_process_tenant_active_apps)", "tenant {{tenant_id}}")],
        "short",
    )

    b.row("Backlog proxies (change-log lag, dead letters)")
    b.ts(
        "Change-log lag",
        [
            ("max(svc_process_changelog_lag)", "process"),
            ("max(svc_action_changelog_lag)", "action"),
        ],
        "short",
        "safe_seq - last_seq after the latest poll: how far the active-set loader trails hub-api's change log.",
    )
    b.ts(
        "Active-set reconcile duration p95",
        [
            (hq(0.95, "svc_process_changelog_reconcile_duration_seconds"), "process"),
            (hq(0.95, "svc_action_changelog_reconcile_duration_seconds"), "action"),
        ],
        "s",
    )
    b.ts(
        "Change-log gaps / retention exceeded / stale evictions",
        [
            (increase("svc_process_changelog_gap_detected_total"), "process gap"),
            (increase("svc_action_changelog_gap_detected_total"), "action gap"),
            (
                increase("svc_process_changelog_retention_exceeded_total"),
                "process retention",
            ),
            (
                increase("svc_action_changelog_retention_exceeded_total"),
                "action retention",
            ),
            (
                increase("svc_process_changelog_scope_stale_evicted_total"),
                "process stale evicted",
            ),
            (
                increase("svc_action_changelog_scope_stale_evicted_total"),
                "action stale evicted",
            ),
        ],
        "short",
        "Each forces a full authoritative reconcile.",
    )
    b.ts(
        "Per-scope read / resolve failures",
        [
            (
                sumrate("svc_process_changelog_scope_failures_total", by="reason"),
                "process {{reason}}",
            ),
            (
                sumrate("svc_action_changelog_scope_failures_total", by="reason"),
                "action {{reason}}",
            ),
        ],
        "ops",
        "read_failed is the database-connectivity signal for the active-set loader.",
    )
    b.ts(
        "Dead-lettered, no executor",
        [
            (increase("dispatch_dead_lettered_no_executor_total"), "{{job}}"),
        ],
        "short",
    )

    b.row("Failures - bundle load and executor link")
    b.ts(
        "Bundle loads by result",
        [
            (
                sumrate("svc_process_bundle_loads_total", by="result"),
                "process {{result}}",
            ),
            (
                sumrate("svc_action_bundle_loads_total", by="result"),
                "action {{result}}",
            ),
        ],
        "ops",
    )
    b.ts(
        "Forced full syncs by reason",
        [
            (
                sumrate("svc_process_bundle_full_sync_total", by="reason"),
                "process {{reason}}",
            ),
            (
                sumrate("svc_action_bundle_full_sync_total", by="reason"),
                "action {{reason}}",
            ),
        ],
        "ops",
    )
    b.ts(
        "Heartbeat timeouts / executor reconnects / zero-session",
        [
            (
                increase("host_api_heartbeat_timeouts_total"),
                "heartbeat timeouts {{job}}",
            ),
            (
                increase("svc_process_executor_reconnect_detected_total"),
                "process reconnects",
            ),
            (
                increase("svc_action_executor_reconnect_detected_total"),
                "action reconnects",
            ),
            (increase("svc_process_bundle_zero_session_total"), "process zero-session"),
            (increase("svc_action_bundle_zero_session_total"), "action zero-session"),
        ],
        "short",
    )
    b.ts(
        "Bundles loaded on executor",
        [("max(svc_process_bundles_loaded)", "process")],
        "short",
    )
    b.ts(
        "Active-set rows excluded (top 10 app/reason)",
        [
            (
                f"topk(10, sum by (app_id, reason) ({increase('svc_process_bundle_active_set_excluded_total')}))",
                "process {{app_id}} {{reason}}",
            ),
            (
                f"topk(10, sum by (app_id, reason) ({increase('svc_action_bundle_active_set_excluded_total')}))",
                "action {{app_id}} {{reason}}",
            ),
        ],
        "short",
        "An active bundle silently going dark (approval expired, digest missing).",
    )

    b.row("egress-proxy")
    b.ts(
        "Requests by mode / decision",
        [
            (
                sumrate("egress_proxy_requests_total", by="mode, decision"),
                "{{mode}} {{decision}}",
            )
        ],
        "ops",
        stack=True,
    )
    b.ts(
        "Dial latency p50/p95/p99",
        quantiles("egress_proxy_request_duration_seconds"),
        "s",
        "Validate + resolve + dial the upstream.",
    )

    b.http_red("svc-ingest", "svc_ingest")
    b.http_red("svc-process", "svc_process")
    b.http_red("svc-action", "svc_action")
    return b


def streaming_board() -> Board:
    """svc_streaming: ingest, HLS/WebRTC/relay/record egress, and the stream latency histograms."""
    b = Board(
        "waddles-streaming",
        "Waddles / svc-streaming",
        "Streaming data plane. stream_* series are OTLP-only (stream latency PR #758); rtc_/rtmp_/hls_/recording_/"
        "relay series are scraped from /metrics (:9090).",
    )
    b.row("Overview")
    b.stat(
        "Active pumped sessions",
        "sum(stream_active_sessions)",
        "short",
        "RTMP/SRT sessions live now (stream_active_sessions, OTLP, PR #758).",
    )
    b.stat(
        "WebRTC sessions",
        "sum(rtc_sessions_active)",
        "short",
        "Active WHIP + WHEP sessions (rtc_sessions_active).",
    )
    b.stat(
        "Session failures, 15m",
        "sum(increase(stream_session_failures_total[15m]))",
        "short",
        "Accepted ingest sessions that never became a running pipeline (PR #758).",
        [(None, "green"), (1, "yellow"), (5, "red")],
    )
    b.stat(
        "ICE failures, 15m",
        "sum(increase(rtc_ice_failures_total[15m]))",
        "short",
        "WHIP/WHEP peer connections that reached Failed.",
        [(None, "green"), (1, "yellow"), (10, "red")],
    )
    b.ts(
        "Sessions started / ended",
        [
            (
                sumrate("stream_sessions_total", by="protocol, outcome"),
                "{{protocol}} {{outcome}}",
            )
        ],
        "ops",
        "Pumped RTMP/SRT ingest session lifecycle (PR #758).",
    )
    b.ts(
        "Session failures by protocol / reason",
        [
            (
                sumrate("stream_session_failures_total", by="protocol, reason"),
                "{{protocol}} {{reason}}",
            )
        ],
        "ops",
        "config_not_found, tenant_unresolved, spec_build_failed, engine_start_failed, whip_sdp_missing, "
        "stdin_unavailable, no_database (PR #758).",
        stack=True,
    )

    b.row("Stream latency (PR #758 histograms)")
    b.ts(
        "Time to first egress p50/p95/p99",
        quantiles("stream_time_to_first_egress_seconds", by="protocol"),
        "s",
        "Ingest accepted -> first HLS segment published.",
    )
    b.ts(
        "SFU fanout latency p50/p95/p99",
        quantiles("stream_fanout_latency_seconds", by="kind"),
        "s",
        "RTP packet entering the fanout -> a WHEP viewer receiving it (1-in-16 sampled).",
    )
    b.ts(
        "Ingest handoff p99 (backpressure)",
        [(hq(0.99, "stream_ingest_handoff_seconds", by="protocol"), "{{protocol}}")],
        "s",
        "One ingest chunk read -> written to ffmpeg stdin; rises under ffmpeg backpressure.",
    )
    b.ts(
        "Pipeline stage duration p95",
        [(hq(0.95, "stream_stage_duration_seconds", by="stage"), "{{stage}}")],
        "s",
        "db_connect, config_lookup, tenant_resolve, spec_build, egress_start, engine_start, ffmpeg_spawn, "
        "ffmpeg_first_progress, ffmpeg_stop, teardown.",
    )
    b.ts(
        "External call duration p95",
        [
            (
                hq(0.95, "stream_external_call_duration_seconds", by="peer, outcome"),
                "{{peer}} {{outcome}}",
            )
        ],
        "s",
        "token_ledger, ingest_auth, object_store; outcome=error is a transport failure.",
    )
    b.ts(
        "Session / relay lifetime p50/p95",
        [
            (hq(0.5, "stream_session_duration_seconds"), "session p50"),
            (hq(0.95, "stream_session_duration_seconds"), "session p95"),
            (hq(0.5, "stream_relay_session_seconds"), "relay p50"),
            (hq(0.95, "stream_relay_session_seconds"), "relay p95"),
        ],
        "s",
    )

    b.row("HLS segments")
    b.ts(
        "Segment media duration p50/p95",
        [
            (
                hq(0.5, "stream_segment_duration_seconds", by="variant"),
                "p50 {{variant}}",
            ),
            (
                hq(0.95, "stream_segment_duration_seconds", by="variant"),
                "p95 {{variant}}",
            ),
        ],
        "s",
        "From #EXTINF; clustered around the 2-6 s packager target (PR #758).",
    )
    b.ts(
        "Segment size p95",
        [(hq(0.95, "stream_segment_size_bytes", by="variant"), "{{variant}}")],
        "bytes",
    )
    b.ts(
        "Segments per second",
        [
            (sumrate("stream_segments_total", by="variant"), "otel {{variant}}"),
            (sumrate("hls_segments_written_total", by="variant"), "scrape {{variant}}"),
        ],
        "ops",
        "Two independent counters (OTLP and scraped) - they should track each other.",
    )
    b.ts(
        "Playlist age",
        [
            (
                hq(0.95, "stream_hls_playlist_age_seconds", by="variant"),
                "p95 {{variant}}",
            ),
            (
                "max by (variant, profile) (hls_playlist_age_s)",
                "now {{variant}}/{{profile}}",
            ),
        ],
        "s",
        "Stale playlist = encoder stalled; hls_playlist_age_s is the live gauge.",
    )
    b.ts(
        "HLS bytes written",
        [(sumrate("hls_bytes_total", by="variant"), "{{variant}}")],
        "Bps",
    )
    b.ts(
        "Ingest bytes into ffmpeg",
        [(sumrate("stream_ingest_bytes_total", by="protocol"), "{{protocol}}")],
        "Bps",
    )

    b.row("Ingest protocols and WebRTC (scraped)")
    b.ts(
        "RTMP connections",
        [("sum(rtmp_connections_active)", "open connections")],
        "short",
    )
    b.ts(
        "RTMP publish results",
        [(sumrate("rtmp_publish_total", by="result"), "{{result}}")],
        "ops",
        stack=True,
    )
    b.ts("RTMP bytes", [(sumrate("rtmp_bytes_total"), "payload bytes")], "Bps")
    b.ts(
        "WebRTC sessions by kind",
        [("sum by (kind) (rtc_sessions_active)", "{{kind}}")],
        "short",
    )
    b.ts(
        "RTP packets by direction",
        [(sumrate("rtc_rtp_packets_total", by="direction"), "{{direction}}")],
        "pps",
        "ingress (WHIP in), egress (WHEP out), transcoded_ingest (ffmpeg rtp leg).",
    )
    b.ts(
        "Fanout drops and ICE failures",
        [
            (sumrate("rtc_packets_dropped_total"), "packets dropped (slow subscriber)"),
            (sumrate("rtc_ice_failures_total"), "ICE failures"),
        ],
        "ops",
    )

    b.row("Relay and recording egress")
    b.ts(
        "Relay targets active",
        [("sum(svc_streaming_relay_targets_active)", "targets")],
        "short",
    )
    b.ts(
        "Relay target failures by reason",
        [
            (
                sumrate("svc_streaming_relay_target_failures_total", by="reason"),
                "{{reason}}",
            )
        ],
        "ops",
        stack=True,
    )
    b.ts(
        "Relay bytes forwarded",
        [(sumrate("svc_streaming_relay_bytes_total"), "all targets")],
        "Bps",
    )
    b.ts(
        "Recording segments uploaded / failed",
        [
            (sumrate("recording_segments_uploaded_total"), "uploaded"),
            (
                sumrate("recording_upload_failures_total", by="reason"),
                "failed {{reason}}",
            ),
        ],
        "ops",
    )
    b.ts(
        "Recording spool backlog",
        [("sum(recording_spool_bytes)", "spool")],
        "bytes",
        "Bytes waiting for upload to object storage.",
    )
    b.ts(
        "Recording upload duration p50/p95",
        [
            (
                hq(0.5, "recording_upload_duration_seconds", by="outcome"),
                "p50 {{outcome}}",
            ),
            (
                hq(0.95, "recording_upload_duration_seconds", by="outcome"),
                "p95 {{outcome}}",
            ),
        ],
        "s",
    )
    b.http_red("svc-streaming", "svc_streaming")
    return b


def presentation_board() -> Board:
    """svc_presentation: overlay push / fan-out latency, subscribers, drops and image uploads."""
    b = Board(
        "waddles-presentation",
        "Waddles / svc-presentation",
        "Overlay presentation service: push endpoint latency, in-process fan-out latency, subscriber health, "
        "dropped frames and image uploads. Scraped from /metrics (:9090).",
    )
    fan = "svc_presentation_overlay_fanout_latency_seconds"
    http_dur = "svc_presentation_http_request_duration_seconds"
    push = [
        'method="POST"',
        'path=~"/overlay/[^/]+/[^/]+/push"',
        'path!~"/overlay/[^/]+/image/push"',
    ]
    b.row("Overlay push")
    b.stat(
        "Subscribers",
        "sum(svc_presentation_overlay_subscribers)",
        "short",
        "Connected overlay viewers (SSE + WebSocket).",
    )
    b.stat(
        "Fan-out p99",
        hq(0.99, fan),
        "s",
        "Time to enqueue a published frame onto every subscriber channel.",
        [(None, "green"), (0.05, "yellow"), (0.1, "red")],
    )
    b.stat(
        "Push request p99",
        hq(0.99, http_dur, *push),
        "s",
        "POST /overlay/{community}/{surface}/push end-to-end handler latency.",
        [(None, "green"), (0.5, "yellow"), (1, "red")],
    )
    b.stat(
        "Dropped frames / s",
        sumrate("svc_presentation_overlay_dropped_frames_total"),
        "ops",
        "Frames dropped because a subscriber fell behind the bounded (64-frame) buffer.",
        [(None, "green"), (0.1, "yellow"), (1, "red")],
    )
    b.ts(
        "Push request latency p50/p95/p99",
        quantiles(http_dur, *push),
        "s",
        "Raw-path regex match; the push route is POST-only.",
    )
    b.ts(
        "Fan-out latency p50/p95/p99",
        quantiles(fan),
        "s",
        "In-process broadcast enqueue; healthy values are sub-millisecond.",
    )
    b.ts("Fan-out p99 by surface", [(hq(0.99, fan, by="surface"), "{{surface}}")], "s")
    b.ts(
        "Pushes per second",
        [
            (
                sumrate("svc_presentation_http_requests_total", *push, by="status"),
                "{{status}}",
            )
        ],
        "reqps",
        stack=True,
    )

    b.row("Subscribers and drops")
    b.ts(
        "Subscribers by surface",
        [("sum by (surface) (svc_presentation_overlay_subscribers)", "{{surface}}")],
        "short",
        stack=True,
    )
    b.ts(
        "Dropped frames by surface",
        [
            (
                sumrate("svc_presentation_overlay_dropped_frames_total", by="surface"),
                "{{surface}}",
            )
        ],
        "ops",
        stack=True,
    )
    b.ts(
        "Viewer connection lifetime p50/p95",
        [
            (
                hq(
                    0.5,
                    "svc_presentation_overlay_connection_duration_seconds",
                    by="surface",
                ),
                "p50 {{surface}}",
            ),
            (
                hq(
                    0.95,
                    "svc_presentation_overlay_connection_duration_seconds",
                    by="surface",
                ),
                "p95 {{surface}}",
            ),
        ],
        "s",
        "Short lifetimes mean viewers are reconnecting.",
    )

    b.row("Image uploads")
    b.ts(
        "Uploads by result",
        [(sumrate("svc_presentation_image_uploads_total", by="result"), "{{result}}")],
        "ops",
        "success / rejected / error.",
        stack=True,
    )
    b.ts(
        "Upload size p95",
        [(hq(0.95, "svc_presentation_image_upload_bytes"), "p95")],
        "bytes",
    )
    b.ts(
        "Presign latency p50/p95/p99",
        quantiles("svc_presentation_image_sign_latency_seconds"),
        "s",
        "Presigned GET URL generation.",
    )
    b.http_red("svc-presentation", "svc_presentation")
    return b


def bundle_executor_board() -> Board:
    """Bundle executor link, host-capability call latency and capability-gate denials."""
    b = Board(
        "waddles-bundle-executor",
        "Waddles / bundle executor and capability gate",
        "The bundle-executor binary exports no metrics of its own; this board shows its link as seen by svc-process / "
        "svc-action, the host-capability (kv, db, http) call latency, and every capability / quota / egress denial.",
    )
    b.row("Executor link (observed from svc-process / svc-action)")
    b.stat(
        "Executors connected",
        "sum by (job) (host_api_connected_executors)",
        "short",
        "Live executor sessions per stage; 0 means every dispatch is dead-lettering.",
        [(None, "red"), (1, "green")],
        legend="{{job}}",
    )
    b.stat(
        "Heartbeat timeouts, 1h",
        "sum(increase(host_api_heartbeat_timeouts_total[1h]))",
        "short",
        "Executor sessions dropped after missing consecutive heartbeats.",
        [(None, "green"), (1, "red")],
    )
    b.stat(
        "Dead-lettered, 1h",
        "sum(increase(dispatch_dead_lettered_no_executor_total[1h]))",
        "short",
        "Dispatch entries with no executor connection.",
        [(None, "green"), (1, "red")],
    )
    b.stat(
        "Bundle load failures, 1h",
        '(sum(increase(svc_process_bundle_loads_total{result="failure"}[1h])) or vector(0)) + (sum(increase(svc_action_bundle_loads_total{result="failure"}[1h])) or vector(0))',
        "short",
        "Per-bundle Load failures on the executor.",
        [(None, "green"), (1, "red")],
    )
    b.ts(
        "Executor reconnects and zero-session bundles",
        [
            (
                increase("svc_process_executor_reconnect_detected_total"),
                "process reconnects",
            ),
            (
                increase("svc_action_executor_reconnect_detected_total"),
                "action reconnects",
            ),
            (increase("svc_process_bundle_zero_session_total"), "process zero-session"),
            (increase("svc_action_bundle_zero_session_total"), "action zero-session"),
        ],
        "short",
        "Zero-session = an active bundle found loaded on no live executor (fail-closed).",
    )
    b.ts(
        "Bundle loads by result",
        [
            (
                sumrate("svc_process_bundle_loads_total", by="result"),
                "process {{result}}",
            ),
            (
                sumrate("svc_action_bundle_loads_total", by="result"),
                "action {{result}}",
            ),
        ],
        "ops",
    )

    b.row("Host capability calls (invoke path) - latency and errors")
    kv, db = (
        "waddles_bundle_kv_op_duration_seconds",
        "waddles_bundle_tables_call_duration_seconds",
    )
    b.ts("kv op latency p50/p95/p99", quantiles(kv), "s", BUCKET_NOTE.strip())
    b.ts(
        "kv slow-op share (> 5 s)",
        [(slow_share(kv, "5"), "slow ops")],
        "percentunit",
        BUCKET_NOTE.strip(),
    )
    b.ts(
        "kv ops by op / outcome",
        [(sumrate(kv + "_count", by="op, outcome"), "{{op}} {{outcome}}")],
        "ops",
        stack=True,
    )
    b.ts(
        "kv errors by kind",
        [
            (
                sumrate("waddles_bundle_kv_op_errors_total", by="op, kind"),
                "{{op}} {{kind}}",
            )
        ],
        "ops",
        "invalid_key, too_large, not_granted, backend (Valkey connectivity), ...",
        stack=True,
    )
    b.ts("db call latency p50/p95/p99", quantiles(db), "s", BUCKET_NOTE.strip())
    b.ts(
        "db slow-call share (> 5 s)",
        [(slow_share(db, "5"), "slow calls")],
        "percentunit",
        BUCKET_NOTE.strip(),
    )
    b.ts(
        "db calls by op / result",
        [
            (
                sumrate("waddles_bundle_tables_calls_total", by="op, result"),
                "{{op}} {{result}}",
            )
        ],
        "ops",
        "result: ok, invalid_column, not_found, conflict, quota_exceeded, timeout, backend, not_granted.",
        stack=True,
    )
    b.ts(
        "db rows per op (p95)",
        [(hq(0.95, "waddles_bundle_tables_rows", by="op"), "{{op}}")],
        "short",
        "Rows returned / affected, never row content.",
    )

    b.row("Capability gate - denials")
    union = (
        f"({sumrate('waddles_bundle_kv_authorize_denied_total')} or vector(0)) + "
        f"({sumrate('waddles_bundle_tables_authorize_denied_total')} or vector(0)) + "
        f"({sumrate('waddles_bundle_quota_denied_total')} or vector(0)) + "
        f"({sumrate('waddles_bundle_kv_quota_rejections_total')} or vector(0)) + "
        f"({sumrate('svc_process_egress_denied_total')} or vector(0)) + "
        f"({sumrate('svc_action_egress_denied_total')} or vector(0)) + "
        f"({sumrate('waddles_bundle_capability_denied_total')} or vector(0))"
    )
    b.ts(
        "Total denial rate",
        [(union, "all denials")],
        "ops",
        "Sum of every denial counter below. waddles_bundle_capability_denied_total (central gate) is a "
        "`metrics`-facade counter with no recorder installed in svc-process/svc-action yet, so it contributes 0 "
        "until one is wired - the per-capability counters carry the signal.",
        w=24,
    )
    b.ts(
        "authorize() denials by app / permission (top 10)",
        [
            (
                f"topk(10, sum by (app_id, permission) ({rate('waddles_bundle_kv_authorize_denied_total')}))",
                "kv {{app_id}} {{permission}}",
            ),
            (
                f"topk(10, sum by (app_id, permission) ({rate('waddles_bundle_tables_authorize_denied_total')}))",
                "tables {{app_id}} {{permission}}",
            ),
        ],
        "ops",
        "Undeclared storage.kv / storage.tables grants.",
    )
    b.ts(
        "Quota rejections",
        [
            (
                sumrate("waddles_bundle_quota_denied_total", by="quota_kind"),
                "db {{quota_kind}}",
            ),
            (
                sumrate("waddles_bundle_kv_quota_rejections_total", by="quota"),
                "kv {{quota}}",
            ),
        ],
        "ops",
        stack=True,
    )
    b.ts(
        "Egress guard denials by reason",
        [
            (
                sumrate("svc_process_egress_denied_total", by="reason"),
                "process {{reason}}",
            ),
            (
                sumrate("svc_action_egress_denied_total", by="reason"),
                "action {{reason}}",
            ),
        ],
        "ops",
        "Bundle http.send calls denied or rate-limited by the egress guard.",
        stack=True,
    )
    b.ts(
        "Central gate: denied vs authorized (when wired)",
        [
            (
                sumrate("waddles_bundle_capability_denied_total", by="reason"),
                "denied {{reason}}",
            ),
            (sumrate("waddles_bundle_capability_authorized_total"), "authorized"),
        ],
        "ops",
        "Empty until a `metrics` recorder is installed (see README, instrumentation gaps).",
    )
    b.ts(
        "Flag evaluations by result",
        [
            (
                sumrate("svc_process_flags_evaluated_total", by="result"),
                "process {{result}}",
            ),
            (
                sumrate("svc_action_flags_evaluated_total", by="result"),
                "action {{result}}",
            ),
        ],
        "ops",
        "live / cached / default / bypass / no_client / capability_disabled.",
        stack=True,
    )
    return b


BOARDS = {
    "waddles-hub-api.json": hub_api_board,
    "waddles-data-plane.json": data_plane_board,
    "waddles-streaming.json": streaming_board,
    "waddles-presentation.json": presentation_board,
    "waddles-bundle-executor.json": bundle_executor_board,
}


def build_dashboards() -> dict[str, str]:
    """Renders every dashboard to its JSON text, keyed by file name."""
    return {
        name: json.dumps(fn().render(), indent=2) + "\n" for name, fn in BOARDS.items()
    }


PROMQL_WORDS = {
    "by",
    "without",
    "on",
    "ignoring",
    "group_left",
    "group_right",
    "and",
    "or",
    "unless",
    "bool",
    "offset",
    "inf",
    "nan",
    "Inf",
    "NaN",
    "le",
}
HIST_SUFFIXES = ("_bucket", "_sum", "_count")
# Metrics owned by exporters outside this repo (kube-state-metrics, Prometheus, OTel Collector, postgres_exporter).
EXTERNAL_PREFIXES = ("kube_", "otelcol_", "pg_")
EXTERNAL_NAMES = {"up"}
# Emitted only by PR #758 (svc-streaming stream latency); not on the release branch yet. Drop each entry
# once #758 merges - check-metrics warns when a pending name is already present in the tree.
PENDING_758 = {
    "stream_ingest_handoff_seconds",
    "stream_time_to_first_egress_seconds",
    "stream_fanout_latency_seconds",
    "stream_segment_duration_seconds",
    "stream_segment_size_bytes",
    "stream_hls_playlist_age_seconds",
    "stream_stage_duration_seconds",
    "stream_external_call_duration_seconds",
    "stream_relay_session_seconds",
    "stream_session_duration_seconds",
    "stream_sessions_total",
    "stream_session_failures_total",
    "stream_ingest_bytes_total",
    "stream_segments_total",
    "stream_active_sessions",
}


def _expand_name_regex(pattern: str) -> set[str]:
    """Expands ``a(_x)?b`` style optional groups of a ``__name__=~`` matcher into concrete names."""
    names = {pattern}
    while any("(" in n for n in names):
        nxt: set[str] = set()
        for n in names:
            m = re.search(r"\(([^()]*)\)\?", n)
            if not m:
                nxt.add(n)
                continue
            nxt.add(n[: m.start()] + n[m.end() :])
            nxt.add(n[: m.start()] + m.group(1) + n[m.end() :])
        names = nxt
    return names


def metric_names(expr: str) -> set[str]:
    """Metric names referenced by a PromQL expression (plain selectors and ``__name__`` regexes)."""
    names: set[str] = set()
    e = re.sub(r"\$\{?[A-Za-z_]+\}?", "", expr)
    for m in re.finditer(r'__name__\s*=~\s*"([^"]+)"', e):
        names |= _expand_name_regex(m.group(1))
    e = re.sub(r'"(?:[^"\\]|\\.)*"', '""', e)
    e = re.sub(r"\[[^\]]*\]", "", e)
    e = re.sub(r"\{[^}]*\}", "", e)
    e = re.sub(
        r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", " ", e
    )
    for m in re.finditer(r"(?<![A-Za-z0-9_:.])[A-Za-z_:][A-Za-z0-9_:]*", e):
        tok = m.group(0)
        if e[m.end() :].lstrip().startswith("(") or tok in PROMQL_WORDS:
            continue
        names.add(tok)
    return names


def dashboard_exprs() -> list[tuple[str, str]]:
    """(label, expr) for every query in every dashboard."""
    out: list[tuple[str, str]] = []
    for name, text in build_dashboards().items():
        for panel in json.loads(text)["panels"]:
            for t in panel.get("targets", []):
                out.append((f"{name}:{panel['title']}", t["expr"]))
    return out


def rule_exprs() -> tuple[list[tuple[str, str]], set[str]]:
    """(label, expr) for every rule, plus the set of recording-rule names."""
    doc = yaml.safe_load(RULES_FILE.read_text())
    exprs: list[tuple[str, str]] = []
    recorded: set[str] = set()
    for group in doc["groups"]:
        for rule in group["rules"]:
            label = rule.get("alert") or rule["record"]
            exprs.append((f"{group['name']}:{label}", rule["expr"]))
            if "record" in rule:
                recorded.add(rule["record"])
    return exprs, recorded


def source_literals() -> set[str]:
    """Normalized string literals from the Rust / Python sources (dots folded to underscores)."""
    lits: set[str] = set()
    pat = re.compile(r'"([A-Za-z][A-Za-z0-9_.]{3,})"')
    for sub, glob in (("core", "*.rs"), ("hub_api", "*.py"), ("libs", "*.py")):
        for path in (REPO / sub).rglob(glob):
            if any(
                part in {"target", "node_modules", ".worktrees", "tests"}
                for part in path.parts
            ):
                continue
            lits.update(
                m.replace(".", "_")
                for m in pat.findall(path.read_text(errors="ignore"))
            )
    return lits


def base_candidates(name: str) -> set[str]:
    """Source-literal spellings a Prometheus series name could have been derived from."""
    base = name
    for s in HIST_SUFFIXES:
        if base.endswith(s):
            base = base[: -len(s)]
            break
    cands = {base, base.removesuffix("_total")}
    for unit in ("_milliseconds", "_seconds", "_bytes"):
        cands |= {c.removesuffix(unit) for c in cands}
    return cands


def cmd_check_metrics() -> int:
    """Fails when a dashboard/alert references a metric the repo does not emit."""
    exprs, recorded = rule_exprs()
    exprs = dashboard_exprs() + exprs
    lits = source_literals()
    unknown: dict[str, list[str]] = {}
    pending_used: set[str] = set()
    examined: set[str] = set()
    for label, expr in exprs:
        for name in metric_names(expr):
            if (
                name in recorded
                or name in EXTERNAL_NAMES
                or name.startswith(EXTERNAL_PREFIXES)
            ):
                continue
            examined.add(name)
            cands = base_candidates(name)
            if cands & PENDING_758:
                pending_used |= cands & PENDING_758
                continue
            if not cands & lits:
                unknown.setdefault(name, []).append(label)
    stale = [n for n in sorted(PENDING_758) if n in lits]
    print(
        f"check-metrics: {len(exprs)} expressions, {len(examined)} distinct repo metrics, "
        f"{len(recorded)} recording rules, {len(pending_used)} pending-#758 metrics, {len(lits)} source literals scanned"
    )
    if stale:
        print(
            f"WARN pending-#758 names now present in the tree (drop from PENDING_758): {stale}"
        )
    if not exprs or not examined or len(lits) < 100:
        print("FAIL: nothing (or implausibly little) was examined - wrong repo root?")
        return 1
    if unknown:
        for name, where in sorted(unknown.items()):
            print(f"FAIL unknown metric {name!r} used in: {sorted(set(where))[:3]}")
        return 1
    print("check-metrics: OK")
    return 0


def cmd_promql_rules(out: Path) -> int:
    """Writes every dashboard query as a recording rule so promtool can syntax-check it."""
    rules = []
    for i, (label, expr) in enumerate(dashboard_exprs()):
        rules.append(
            {
                "record": f"dashcheck:q{i}",
                "expr": expr.replace(RATE, "5m"),
                "labels": {"src": label},
            }
        )
    out.write_text(
        yaml.safe_dump(
            {"groups": [{"name": "dashboard-queries", "rules": rules}]},
            sort_keys=False,
            width=10_000,
        )
    )
    print(f"promql-rules: wrote {len(rules)} dashboard queries to {out}")
    return 0 if rules else 1


def render_prometheusrule() -> str:
    """The PrometheusRule CRD manifest wrapping the plain rule file's groups."""
    doc = yaml.safe_load(RULES_FILE.read_text())
    manifest = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {
            "name": "waddles-slo",
            "namespace": "waddlebot",
            "labels": {
                "app.kubernetes.io/name": "waddlebot",
                "app.kubernetes.io/part-of": "waddles",
                "app.kubernetes.io/component": "observability",
            },
        },
        "spec": {"groups": doc["groups"]},
    }
    header = (
        "# GENERATED from alerts/waddles-slo.rules.yml by scripts/obs_tool.py - do not edit.\n"
        "# Regenerate: make -C k8s/observability render-rules\n"
    )
    return header + yaml.safe_dump(manifest, sort_keys=False, width=10_000)


def main() -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dashboards")
    d.add_argument("--out", type=Path, default=DASH_DIR)
    p = sub.add_parser("prometheusrule")
    p.add_argument("--out", type=Path, default=RULE_MANIFEST)
    q = sub.add_parser("promql-rules")
    q.add_argument("--out", type=Path, required=True)
    sub.add_parser("check-metrics")
    args = ap.parse_args()
    if args.cmd == "dashboards":
        args.out.mkdir(parents=True, exist_ok=True)
        files = build_dashboards()
        for name, text in files.items():
            (args.out / name).write_text(text)
        print(f"dashboards: wrote {len(files)} files to {args.out}")
        return 0
    if args.cmd == "prometheusrule":
        args.out.write_text(render_prometheusrule())
        print(f"prometheusrule: wrote {args.out}")
        return 0
    if args.cmd == "promql-rules":
        return cmd_promql_rules(args.out)
    return cmd_check_metrics()


if __name__ == "__main__":
    sys.exit(main())
