"""Email notifier — sends cleanup reports via SMTP."""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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


class EmailNotifier(BaseNotifier):
    """Sends a formatted HTML cleanup report via SMTP."""

    def __init__(self) -> None:
        self.smtp_host   = os.environ.get("SMTP_HOST", "").strip()
        self.smtp_port   = int(os.environ.get("SMTP_PORT", "587"))
        self.sender      = os.environ.get("SMTP_SENDER", "").strip()
        self.recipients  = [
            r.strip()
            for r in os.environ.get("SMTP_RECIPIENTS", "").split(",")
            if r.strip()
        ]
        self.username    = os.environ.get("SMTP_USERNAME", "").strip()
        self.password    = os.environ.get("SMTP_PASSWORD", "").strip()

    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.sender and self.recipients)

    def report(self, payload: dict[str, Any]) -> None:
        if not self.is_configured():
            logger.warning("Email notifier enabled but SMTP settings are incomplete.")
            return

        deleted   = payload.get("deleted_count", 0)
        freed     = payload.get("space_freed_bytes", 0)
        dry_run   = payload.get("dry_run", False)
        resources = payload.get("resources", [])
        ts        = payload.get("timestamp", "")[:19].replace("T", " ")

        mode_label = "Dry-run" if dry_run else "Live cleanup"
        freed_str  = _humanize(freed) if freed else "—"
        subject    = f"Docker Janitor — {mode_label} complete ({deleted} resources)"

        rows = "".join(
            f"<tr><td style='padding:6px 12px;font-family:monospace;font-size:13px'>{r['name']}</td>"
            f"<td style='padding:6px 12px;text-transform:capitalize'>{r['type']}</td>"
            f"<td style='padding:6px 12px;text-align:right'>{r.get('size_human','—')}</td></tr>"
            for r in resources
        )
        table = (
            f"<table border='0' cellspacing='0' cellpadding='0' width='100%' "
            f"style='border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden'>"
            f"<thead><tr style='background:#f9fafb'>"
            f"<th style='padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280'>Resource</th>"
            f"<th style='padding:8px 12px;text-align:left;font-size:11px;text-transform:uppercase;color:#6b7280'>Type</th>"
            f"<th style='padding:8px 12px;text-align:right;font-size:11px;text-transform:uppercase;color:#6b7280'>Size</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
            if rows else "<p style='color:#6b7280'>No resources were deleted.</p>"
        )

        html = f"""
        <html><body style='font-family:system-ui,sans-serif;max-width:600px;margin:0 auto;padding:24px'>
          <h2 style='color:#111827;margin-bottom:4px'>🧹 Docker Janitor</h2>
          <p style='color:#6b7280;font-size:14px;margin-top:0'>{mode_label} · {ts} UTC</p>
          <div style='display:flex;gap:16px;margin:20px 0'>
            <div style='background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:16px;flex:1'>
              <p style='margin:0;font-size:12px;color:#16a34a;text-transform:uppercase;font-weight:600'>Resources removed</p>
              <p style='margin:4px 0 0;font-size:28px;font-weight:700;color:#111827'>{deleted}</p>
            </div>
            <div style='background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:16px;flex:1'>
              <p style='margin:0;font-size:12px;color:#2563eb;text-transform:uppercase;font-weight:600'>Space freed</p>
              <p style='margin:4px 0 0;font-size:28px;font-weight:700;color:#111827'>{freed_str}</p>
            </div>
          </div>
          <h3 style='color:#374151;font-size:14px;margin-bottom:8px'>Deleted resources</h3>
          {table}
          <p style='color:#9ca3af;font-size:12px;margin-top:24px'>Sent by Docker Janitor</p>
        </body></html>
        """

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = self.sender
        msg["To"]      = ", ".join(self.recipients)
        msg.attach(MIMEText(html, "html"))

        try:
            if self.smtp_port == 465:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=15) as server:
                    if self.username:
                        server.login(self.username, self.password)
                    server.sendmail(self.sender, self.recipients, msg.as_string())
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
                    server.ehlo()
                    server.starttls()
                    if self.username:
                        server.login(self.username, self.password)
                    server.sendmail(self.sender, self.recipients, msg.as_string())
            logger.info("Email notification sent to %s.", self.recipients)
        except Exception as exc:  # noqa: BLE001
            logger.error("Email notification failed: %s", exc)
