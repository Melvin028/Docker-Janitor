"""Generic webhook notifier — POSTs a JSON report payload to a configured URL."""

from __future__ import annotations

import os
from typing import Any

import requests

from janitor.notifier.base import BaseNotifier
from janitor.utils.logger import get_logger

logger = get_logger(__name__)


class WebhookNotifier(BaseNotifier):
    """Sends a JSON-encoded report to a generic HTTP webhook endpoint."""

    def __init__(self) -> None:
        self.url   = os.environ.get("JANITOR_WEBHOOK_URL", "").strip()
        self.token = os.environ.get("JANITOR_WEBHOOK_TOKEN", "").strip()

    def is_configured(self) -> bool:
        return bool(self.url)

    def report(self, payload: dict[str, Any]) -> None:
        if not self.is_configured():
            logger.warning("Webhook notifier enabled but JANITOR_WEBHOOK_URL is not set.")
            return

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            resp = requests.post(self.url, json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            logger.info("Webhook notification sent (status %s).", resp.status_code)
        except requests.RequestException as exc:
            logger.error("Webhook notification failed: %s", exc)
