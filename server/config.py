"""Tunable settings. Every value can be overridden with a TRUSTLANE_* environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from trustlane/.env into the environment (real env vars win)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _env(name: str, default: float) -> float:
    return float(os.getenv(f"TRUSTLANE_{name}", default))


@dataclass
class Settings:
    # Location. The phone re-reports its aisle every 15 s; older than this counts as silent.
    location_fresh_s: float = field(default_factory=lambda: _env("LOCATION_FRESH", 45))
    # A shelf station's Bluetooth detection of a phone counts for this long.
    presence_fresh_s: float = 60
    silent_location_points: float = 15   # silent phone was pulled in as a suspect for a pick
    location_mismatch_points: float = 30  # phone claims one aisle, a shelf detected it in another
    basket_heavier_points: float = 35     # exit floor scale: basket weighs more than the scanned items
    high_value_paise: int = 50000         # unresolved item worth more than this alerts staff at once

    # Matching: a shelf pick must be met by a phone scan within this window, or the phone is nudged.
    match_window_s: float = field(default_factory=lambda: _env("MATCH_WINDOW", 60))
    # A shopper counts as "in the aisle" for this long after their last location update.
    attribution_window_s: float = field(default_factory=lambda: _env("ATTRIBUTION_WINDOW", 180))
    # A put-back can undo a scanned item taken up to this long ago.
    return_lookback_s: float = field(default_factory=lambda: _env("RETURN_LOOKBACK", 1800))

    # Risk points. Decision: green below green_max, amber below amber_max, red otherwise.
    theft_points: dict[str, float] = field(default_factory=lambda: {"low": 10, "medium": 20, "high": 35})
    nudge_points: float = 5
    ignored_nudge_points: float = 20
    swap_points: float = 45
    resolved_swap_points: float = 10
    package_mismatch_points: float = 60
    package_min_confidence: float = 0.8
    tamper_points: float = 100
    green_max: float = field(default_factory=lambda: _env("GREEN_MAX", 25))
    green_max_trusted: float = field(default_factory=lambda: _env("GREEN_MAX_TRUSTED", 35))
    trusted_after_clean_trips: int = 5
    amber_max: float = field(default_factory=lambda: _env("AMBER_MAX", 60))

    token_ttl_s: int = 300

    # Shelf camera (YOLO). Below this confidence the camera's opinion is logged but not used.
    vision_min_confidence: float = field(default_factory=lambda: _env("VISION_MIN_CONFIDENCE", 0.6))
    # The camera may override the lane's product only if the weight change fits the seen product
    # within this fraction of one unit, and fits it better than the lane's product.
    vision_weight_tolerance: float = 0.25

    # Monitoring alert thresholds (proposed starting values, see the deck's monitoring table).
    station_silent_s: float = 120
    match_rate_alert: float = 0.95
    red_cleared_alert: float = 0.30
    latency_p95_alert_s: float = 2.0
    vision_disagree_alert: float = 0.10  # camera and weight disagree on more than 10% of picks
    nudge_spike_factor: float = 2.0      # nudge rate in the last 15 min vs. the long-run rate
    red_exit_alert: float = 0.03         # red on more than 3% of exits
    scale_drift_alert_g: float = 5.0
    offline_buffer_alert: int = 500
    payment_success_alert: float = 0.99
    camera_fps_alert: float = 5.0
    weight_learning_drift: float = 0.08  # learned packaging weight differs from the catalog by 8%
    audit_retention_days: float = field(default_factory=lambda: _env("AUDIT_RETENTION_DAYS", 90))

    # Alert delivery: Slack incoming webhook and/or any webhook (JSON POST). Empty = console only.
    slack_webhook: str = field(default_factory=lambda: os.getenv("TRUSTLANE_SLACK_WEBHOOK", ""))
    alert_webhook: str = field(default_factory=lambda: os.getenv("TRUSTLANE_ALERT_WEBHOOK", ""))

    # Staff PIN for the ops console and staff actions. Empty = a random PIN is generated at start
    # and written to data/staff_pin.txt.
    staff_pin: str = field(default_factory=lambda: os.getenv("TRUSTLANE_STAFF_PIN", ""))

    # MQTT: shelf stations may publish to the store gateway's broker instead of HTTP.
    mqtt_url: str = field(default_factory=lambda: os.getenv("TRUSTLANE_MQTT_URL", ""))

    # Rate limits per client IP.
    rate_per_second: float = 30
    sessions_per_minute: float = 10

    # Ledger database and gate signing key live here; the catalog is read from catalog_path.
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("TRUSTLANE_DATA_DIR", ROOT / "data")))
    catalog_path: Path = field(default_factory=lambda: Path(os.getenv("TRUSTLANE_CATALOG", ROOT / "data" / "catalog.json")))
    station_key: str = field(default_factory=lambda: os.getenv("TRUSTLANE_STATION_KEY", ""))

    # Payments: "auto" uses Razorpay when test keys are set, otherwise the built-in sandbox.
    payments_provider: str = field(default_factory=lambda: os.getenv("TRUSTLANE_PAYMENTS", "auto"))
    razorpay_key_id: str = field(default_factory=lambda: os.getenv("RAZORPAY_KEY_ID", ""))
    razorpay_key_secret: str = field(default_factory=lambda: os.getenv("RAZORPAY_KEY_SECRET", ""))
    razorpay_webhook_secret: str = field(default_factory=lambda: os.getenv("RAZORPAY_WEBHOOK_SECRET", ""))
