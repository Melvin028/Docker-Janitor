"""Scan history store — persists lightweight scan snapshots for trend charting."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from janitor.scanner.models import ScanResult
from janitor.utils.logger import get_logger

logger = get_logger(__name__)

HISTORY_PATH = "logs/scan_history.jsonl"


def _humanize(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / (1024 ** 3):.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / (1024 ** 2):.1f} MB"
    return f"{n / 1024:.0f} KB"


def append_scan(scan_result: ScanResult, path: str = HISTORY_PATH) -> None:
    """Append a lightweight summary of *scan_result* to the history log."""
    du = scan_result.disk_usage
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "images_total":       len(scan_result.images),
        "images_unused":      len(scan_result.unused_images),
        "containers_total":   len(scan_result.containers),
        "containers_stopped": len(scan_result.stopped_containers),
        "volumes_total":      len(scan_result.volumes),
        "volumes_unused":     len(scan_result.unused_volumes),
        "total_bytes":        du.total_bytes,
        "images_bytes":       du.images_bytes,
        "containers_bytes":   du.containers_bytes,
        "volumes_bytes":      du.volumes_bytes,
        "build_cache_bytes":  du.build_cache_bytes,
    }
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        logger.debug("Scan history entry written.")
    except OSError as exc:
        logger.warning("Could not write scan history: %s", exc)


def read_history(limit: int = 90, path: str = HISTORY_PATH) -> list[dict]:
    """Return the most recent *limit* scan history entries, oldest first."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [l.strip() for l in fh if l.strip()]
        entries = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
    except OSError as exc:
        logger.warning("Could not read scan history: %s", exc)
        return []


def compute_trend(history: list[dict]) -> dict:
    """Return trend metadata comparing the last two scans.

    Returns a dict with keys:
      direction  – "up" | "down" | "stable"
      pct        – absolute percentage change (float)
      label      – human-readable label e.g. "+3.2% since last scan"
    """
    if len(history) < 2:
        return {"direction": "stable", "pct": 0.0, "label": "No previous data"}

    prev = history[-2]["total_bytes"] or 1
    curr = history[-1]["total_bytes"] or 0
    delta = curr - prev
    pct = abs(delta / prev) * 100

    if pct < 0.5:
        return {"direction": "stable", "pct": pct, "label": "Stable since last scan"}
    if delta > 0:
        return {"direction": "up",   "pct": pct, "label": f"+{pct:.1f}% since last scan"}
    return     {"direction": "down", "pct": pct, "label": f"-{pct:.1f}% since last scan"}
