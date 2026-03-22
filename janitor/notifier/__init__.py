"""Notifier package — delivers cleanup reports via CLI, Slack, webhook, or email."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from janitor.utils.logger import get_logger

logger = get_logger(__name__)


def _humanize(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / (1024 ** 3):.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / (1024 ** 2):.1f} MB"
    return f"{n / 1024:.0f} KB"


def build_payload(
    deleted_items: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build a notification payload from a list of deleted resource dicts.

    Each item in *deleted_items* should have at least:
        name, type, size_bytes (optional), size_human (optional).
    """
    total_freed = sum(r.get("size_bytes", 0) for r in deleted_items)
    return {
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "deleted_count":     len(deleted_items),
        "space_freed_bytes": total_freed,
        "space_freed_human": _humanize(total_freed) if total_freed else "—",
        "dry_run":           dry_run,
        "resources":         deleted_items,
    }


def send_notifications(cfg: dict[str, Any], payload: dict[str, Any]) -> None:
    """Fire every enabled notifier with *payload*.

    Failures in individual notifiers are caught and logged; they never
    propagate to the caller.
    """
    notif_cfg: dict[str, Any] = cfg.get("notifications") or {}

    # CLI reporter (always fires when enabled, defaults to true)
    if (notif_cfg.get("cli") or {}).get("enabled", True):
        try:
            from janitor.notifier.cli_reporter import CliReporter
            CliReporter().report(payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("CLI reporter failed: %s", exc)

    # Slack
    if (notif_cfg.get("slack") or {}).get("enabled", False):
        try:
            from janitor.notifier.slack import SlackNotifier
            SlackNotifier().report(payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("Slack notifier failed: %s", exc)

    # Generic webhook
    if (notif_cfg.get("webhook") or {}).get("enabled", False):
        try:
            from janitor.notifier.webhook import WebhookNotifier
            WebhookNotifier().report(payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("Webhook notifier failed: %s", exc)

    # Email
    if (notif_cfg.get("email") or {}).get("enabled", False):
        try:
            from janitor.notifier.email import EmailNotifier
            EmailNotifier().report(payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("Email notifier failed: %s", exc)
