"""Slack notifier — posts cleanup reports to a Slack channel via incoming webhook."""

from __future__ import annotations

import os
from typing import Any

import requests

from janitor.notifier.base import BaseNotifier
from janitor.utils.logger import get_logger

logger = get_logger(__name__)


def _humanize(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / (1024 ** 3):.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / (1024 ** 2):.1f} MB"
    return f"{n / 1024:.0f} KB"


class SlackNotifier(BaseNotifier):
    """Sends a cleanup summary to Slack using an incoming webhook URL."""

    def __init__(self) -> None:
        self.webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    def report(self, payload: dict[str, Any]) -> None:
        if not self.is_configured():
            logger.warning("Slack notifier enabled but SLACK_WEBHOOK_URL is not set.")
            return

        deleted   = payload.get("deleted_count", 0)
        freed     = payload.get("space_freed_bytes", 0)
        dry_run   = payload.get("dry_run", False)
        resources = payload.get("resources", [])
        ts        = payload.get("timestamp", "")

        mode_label = "Dry-run" if dry_run else "Live cleanup"
        header     = f"🧹 Docker Janitor — {mode_label} complete"
        freed_str  = _humanize(freed) if freed else "—"

        # Build resource lines (cap at 10 to keep message compact)
        lines = [
            f"• `{r['name']}` ({r['type']}) — {r.get('size_human', '—')}"
            for r in resources[:10]
        ]
        if len(resources) > 10:
            lines.append(f"…and {len(resources) - 10} more")

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": header, "emoji": True},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Resources removed*\n{deleted}"},
                    {"type": "mrkdwn", "text": f"*Space freed*\n{freed_str}"},
                    {"type": "mrkdwn", "text": f"*Mode*\n{mode_label}"},
                    {"type": "mrkdwn", "text": f"*Time*\n{ts[:19].replace('T', ' ')} UTC"},
                ],
            },
        ]

        if lines:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Deleted resources*\n" + "\n".join(lines)},
            })

        blocks.append({"type": "divider"})

        try:
            resp = requests.post(
                self.webhook_url,
                json={"blocks": blocks},
                timeout=10,
            )
            resp.raise_for_status()
            logger.info("Slack notification sent (status %s).", resp.status_code)
        except requests.RequestException as exc:
            logger.error("Slack notification failed: %s", exc)
