"""CLI reporter — logs a cleanup summary to the application logger."""

from __future__ import annotations

from typing import Any

from janitor.notifier.base import BaseNotifier
from janitor.utils.logger import get_logger

logger = get_logger(__name__)


def _humanize(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / (1024 ** 3):.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / (1024 ** 2):.1f} MB"
    return f"{n / 1024:.0f} KB"


class CliReporter(BaseNotifier):
    """Renders a cleanup report as structured log lines."""

    def report(self, payload: dict[str, Any]) -> None:
        deleted  = payload.get("deleted_count", 0)
        freed    = payload.get("space_freed_bytes", 0)
        dry_run  = payload.get("dry_run", False)
        mode     = "DRY-RUN" if dry_run else "LIVE"

        logger.info("─" * 60)
        logger.info("Docker Janitor — %s cleanup report", mode)
        logger.info("  Resources removed : %d", deleted)
        logger.info("  Space freed       : %s", _humanize(freed) if freed else "—")

        for r in payload.get("resources", []):
            logger.info("  • %-50s %s", r["name"], r.get("size_human", ""))

        logger.info("─" * 60)
