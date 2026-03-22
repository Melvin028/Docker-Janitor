"""Append-only JSONL audit log for all cleanup actions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AUDIT_LOG_PATH = Path("logs/audit.jsonl")


def append_entry(entry: dict[str, Any]) -> None:
    """Append a single audit entry to the log file (creates directories as needed)."""
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def read_entries(limit: int = 500) -> list[dict[str, Any]]:
    """Return up to *limit* entries from the log, newest first."""
    if not AUDIT_LOG_PATH.exists():
        return []
    with AUDIT_LOG_PATH.open("r", encoding="utf-8") as f:
        raw_lines = f.readlines()

    entries: list[dict[str, Any]] = []
    for line in reversed(raw_lines):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(entries) >= limit:
            break
    return entries


def clear_log() -> None:
    """Delete all entries from the audit log."""
    if AUDIT_LOG_PATH.exists():
        AUDIT_LOG_PATH.unlink()


def make_entry(
    *,
    resource_id: str,
    resource_type: str,
    display_name: str,
    size_bytes: int,
    action: str,
    dry_run: bool,
    success: bool,
    message: str,
    reason: str,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Build a typed audit entry dict ready to be passed to :func:`append_entry`.

    ``tags`` — for image resources, the full list of repo:tag strings captured
    *before* deletion so the image can be re-pulled later if needed.
    """
    resolved_tags = tags or []
    # An image is recoverable when it has at least one registry-style tag.
    # Local-only builds have no tags or only sha256 refs — mark them non-recoverable.
    recoverable = bool(
        resource_type == "image"
        and any(t and not t.startswith("sha256:") for t in resolved_tags)
    )
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "resource_id": resource_id,
        "resource_type": resource_type,
        "display_name": display_name,
        "size_bytes": size_bytes,
        "action": action,
        "dry_run": dry_run,
        "success": success,
        "message": message,
        "reason": reason,
        "tags": resolved_tags,
        "pull_commands": [f"docker pull {t}" for t in resolved_tags if t and not t.startswith("sha256:")],
        "recoverable": recoverable,
    }
