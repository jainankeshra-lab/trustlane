"""Prometheus metrics, scraped from /metrics (Grafana reads them from Prometheus)."""
from prometheus_client import Counter, Gauge, Histogram

PICKS = Counter("trustlane_shelf_picks_total", "Shelf take events", ["attributed"])
RETURNS = Counter("trustlane_shelf_returns_total", "Shelf put-back events")
SCANS = Counter("trustlane_scans_total", "Phone scans accepted", ["monitored"])
SCANS_REJECTED = Counter("trustlane_scans_rejected_total", "Phone scans rejected", ["reason"])
MATCHES = Counter("trustlane_pick_scan_matches_total", "Shelf picks matched to a phone scan")
NUDGES = Counter("trustlane_nudges_total", "Nudges sent to phones")
FLAGS = Counter("trustlane_risk_flags_total", "Risk flags raised", ["kind"])
EXIT_DECISIONS = Counter("trustlane_exit_decisions_total", "Exit gate decisions", ["decision"])
RED_RESOLVED = Counter("trustlane_red_checks_resolved_total", "Staff checks resolved", ["outcome"])
PAYMENTS = Counter("trustlane_payments_total", "Exit payments", ["status"])
VISION_CHECKS = Counter("trustlane_vision_checks_total",
                        "Shelf camera vs. load-cell lane on each take", ["result"])
ACTIVE_SESSIONS = Gauge("trustlane_active_sessions", "Shoppers currently in store")
OPEN_DISCREPANCIES = Gauge("trustlane_open_discrepancies", "Picks past the match window with no scan")
STATION_LAST_SEEN = Gauge("trustlane_station_last_seen_seconds", "Unix time a shelf station last reported", ["station"])
EVENT_LATENCY = Histogram(
    "trustlane_shelf_event_latency_seconds", "Shelf event time to server receive time",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
