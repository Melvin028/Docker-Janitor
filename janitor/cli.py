"""CLI entry point for Docker Janitor."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import click

from janitor.config import load_config


# ── formatting helpers ──────────────────────────────────────────────────────

def _h(n: int) -> str:
    """Humanize bytes → compact string."""
    if n >= 1024 ** 3:
        return f"{n / (1024 ** 3):.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / (1024 ** 2):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _rule(width: int = 58, char: str = "─") -> None:
    click.echo(click.style(char * width, fg="bright_black"))


def _header(title: str) -> None:
    click.echo()
    click.echo(click.style("  Docker Janitor", fg="blue", bold=True) +
               click.style(f"  —  {title}", fg="white", bold=True))
    _rule()


def _section(label: str) -> None:
    click.echo()
    click.echo(click.style(f"  {label}", fg="cyan", bold=True))


def _row(label: str, value: str, color: str = "white") -> None:
    click.echo(f"    {click.style(label + ':', fg='bright_black'):<28} "
               f"{click.style(value, fg=color)}")


def _badge(text: str, color: str) -> str:
    return click.style(f" {text} ", fg="black", bg=color)


# ── CLI group ───────────────────────────────────────────────────────────────

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option()
def cli() -> None:
    """Docker Janitor — clean up unused Docker resources."""


# ── scan ────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--config", "-c", default="configs/janitor.yaml",
              show_default=True, help="Path to config file.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def scan(config: str, as_json: bool) -> None:
    """Scan the Docker host and report unused resources."""
    from janitor.scanner.core import Scanner

    try:
        cfg = load_config(config)
    except FileNotFoundError:
        cfg = {}

    click.echo(click.style("  Scanning Docker host…", fg="blue"))

    try:
        result = Scanner(cfg).scan()
    except Exception as exc:  # noqa: BLE001
        click.echo(click.style(f"  Error: {exc}", fg="red"), err=True)
        sys.exit(1)

    if as_json:
        import json
        click.echo(json.dumps({
            "images":     len(result.images),
            "unused_images": len(result.unused_images),
            "containers": len(result.containers),
            "stopped":    len(result.stopped_containers),
            "volumes":    len(result.volumes),
            "orphaned_volumes": len(result.unused_volumes),
            "networks":   len(result.networks),
            "unused_networks": len(result.unused_networks),
            "disk": {
                "total_bytes":       result.disk_usage.total_bytes,
                "images_bytes":      result.disk_usage.images_bytes,
                "containers_bytes":  result.disk_usage.containers_bytes,
                "volumes_bytes":     result.disk_usage.volumes_bytes,
                "build_cache_bytes": result.disk_usage.build_cache_bytes,
                "reclaimable_bytes": result.disk_usage.total_reclaimable_bytes,
            },
        }, indent=2))
        return

    du = result.disk_usage
    _header("Scan results")

    _section("Resources")
    _row("Images",     f"{len(result.images)} total  ·  "
                       f"{len(result.unused_images)} unused  ·  "
                       f"{len(result.dangling_images)} dangling")
    _row("Containers", f"{len(result.containers)} total  ·  "
                       f"{len(result.stopped_containers)} stopped")
    _row("Volumes",    f"{len(result.volumes)} total  ·  "
                       f"{len(result.unused_volumes)} orphaned")
    _row("Networks",   f"{len(result.networks)} total  ·  "
                       f"{len(result.unused_networks)} unused")

    _section("Disk usage")
    _row("Images",      f"{du.images_human:<12}  {_h(du.images_reclaimable_bytes)} reclaimable")
    _row("Containers",  f"{du.containers_human:<12}  {_h(du.containers_reclaimable_bytes)} reclaimable")
    _row("Volumes",     f"{du.volumes_human:<12}  {_h(du.volumes_reclaimable_bytes)} reclaimable")
    _row("Build cache", f"{du.build_cache_human:<12}  {_h(du.build_cache_reclaimable_bytes)} reclaimable")
    _rule()
    _row("Total",       f"{du.total_human:<12}  "
                        + click.style(f"{du.total_reclaimable_human} reclaimable", fg="yellow"),
         color="white")

    # Top unused images
    unused = sorted(result.unused_images, key=lambda i: i.size_bytes, reverse=True)
    if unused:
        _section(f"Largest unused images  ({len(unused)} total)")
        for img in unused[:10]:
            status = (click.style("dangling", fg="red") if img.is_dangling
                      else click.style("unused", fg="yellow"))
            click.echo(f"    {img.display_name:<45} "
                       f"{img.size_human:<10}  {img.age_days}d  [{status}]")
        if len(unused) > 10:
            click.echo(f"    … and {len(unused) - 10} more")

    click.echo()
    if du.total_reclaimable_bytes > 0:
        click.echo(click.style(
            f"  ✓  Run 'docker-janitor clean --live' to free "
            f"{du.total_reclaimable_human}.", fg="green"))
    else:
        click.echo(click.style("  ✓  Everything is clean.", fg="green"))
    click.echo()


# ── clean ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--config", "-c", default="configs/janitor.yaml",
              show_default=True, help="Path to config file.")
@click.option("--dry-run", "mode", flag_value="dry", default=True,
              help="Preview deletions without executing (default).")
@click.option("--live", "mode", flag_value="live",
              help="Execute deletions for real.")
@click.option("--yes", "-y", is_flag=True,
              help="Skip confirmation prompt when running --live.")
def clean(config: str, mode: str, yes: bool) -> None:
    """Evaluate policy and clean up Docker resources.

    Runs in dry-run mode by default. Pass --live to actually delete.
    """
    from janitor.scanner.core import Scanner
    from janitor.policy.engine import PolicyEngine
    from janitor.cleanup.engine import CleanupEngine

    dry_run = (mode != "live")

    try:
        cfg = load_config(config)
    except FileNotFoundError:
        cfg = {}

    label = click.style("DRY-RUN", fg="cyan", bold=True) if dry_run else \
            click.style("LIVE", fg="red", bold=True)
    _header(f"Cleanup  [{label}]")

    click.echo(click.style("  Scanning…", fg="blue"))
    try:
        result = Scanner(cfg).scan()
    except Exception as exc:  # noqa: BLE001
        click.echo(click.style(f"  Error: {exc}", fg="red"), err=True)
        sys.exit(1)

    policy_result = PolicyEngine(cfg).evaluate(result)
    to_delete = [d for d in policy_result.decisions if d.safe_to_delete]

    if not to_delete:
        click.echo()
        click.echo(click.style("  ✓  Nothing to clean up — all resources are within policy.", fg="green"))
        click.echo()
        return

    # Preview table
    _section(f"Resources that {'would be' if dry_run else 'will be'} removed  ({len(to_delete)})")

    # Resolve display names before deletion
    from janitor.scanner.docker_client import get_client
    client = get_client((cfg.get("docker") or {}).get("host"))

    total_bytes = 0
    rows: list[tuple[str, str, int]] = []
    for d in to_delete:
        name = d.resource_id[:12]
        size = 0
        try:
            if d.resource_type == "image":
                img = client.images.get(d.resource_id)
                name = img.tags[0] if img.tags else d.resource_id[:12]
                size = img.attrs.get("Size", 0)
            elif d.resource_type == "container":
                name = client.containers.get(d.resource_id).name
            elif d.resource_type == "volume":
                name = d.resource_id
        except Exception:  # noqa: BLE001
            pass
        rows.append((name, d.resource_type, size))
        total_bytes += size

    for name, rtype, size in rows:
        type_color = {"image": "blue", "container": "magenta",
                      "volume": "yellow", "network": "cyan"}.get(rtype, "white")
        click.echo(f"    {name:<48} "
                   f"{click.style(rtype, fg=type_color):<12}  "
                   f"{_h(size) if size else '—'}")

    click.echo()
    _rule()
    if total_bytes:
        click.echo(click.style(f"  Space to free: {_h(total_bytes)}", fg="yellow"))

    if dry_run:
        click.echo()
        click.echo("  This is a simulation — no changes were made.")
        click.echo(click.style("  Run with --live to execute.", fg="bright_black"))
        click.echo()
        return

    # Confirm before live run
    if not yes:
        click.echo()
        confirmed = click.confirm(
            click.style(f"  Delete {len(to_delete)} resource(s) for real?", fg="red"),
            default=False,
        )
        if not confirmed:
            click.echo("  Aborted.")
            return

    # Execute
    click.echo()
    click.echo(click.style("  Running cleanup…", fg="red"))
    cleanup_result = CleanupEngine(cfg, dry_run=False).execute(policy_result)
    deleted  = cleanup_result.deleted_count
    _rule()
    click.echo(click.style(f"  ✓  Done. {deleted} resource(s) deleted.", fg="green"))
    click.echo()


# ── audit ───────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--limit", "-n", default=20, show_default=True,
              help="Number of entries to show.")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def audit(limit: int, as_json: bool) -> None:
    """Show recent cleanup and recovery actions from the audit log."""
    from janitor.audit.logger import read_entries

    entries = read_entries(limit=limit)

    if as_json:
        import json
        click.echo(json.dumps(entries, indent=2, default=str))
        return

    _header(f"Audit log  (last {min(limit, len(entries))} entries)")

    if not entries:
        click.echo()
        click.echo("  No audit entries yet.")
        click.echo()
        return

    total_freed = sum(
        e.get("size_bytes", 0) for e in entries
        if e.get("success") and not e.get("dry_run") and e.get("action") == "delete"
    )

    _section("Summary")
    _row("Total entries",  str(len(entries)))
    _row("Space freed",    _h(total_freed) if total_freed else "—")
    _row("Failed actions", str(sum(1 for e in entries if not e.get("success"))))

    _section("Entries")
    click.echo(f"    {'Date':<18}  {'Resource':<40}  {'Type':<10}  {'Mode':<8}  Result")
    _rule(72)

    for e in entries:
        ts  = e.get("timestamp", "")[:16].replace("T", " ")
        name = e.get("display_name") or e.get("resource_id", "")[:12]
        rtype = e.get("resource_type", "")
        dry  = e.get("dry_run", False)
        ok   = e.get("success", False)
        act  = e.get("action", "delete")

        mode_str = click.style("dry-run", fg="cyan") if dry else click.style("live", fg="blue")

        if act == "recover":
            result_str = click.style("recovered", fg="green")
        elif ok:
            result_str = click.style("✓ ok", fg="green")
        else:
            result_str = click.style("✗ failed", fg="red")

        type_color = {"image": "blue", "container": "magenta",
                      "volume": "yellow", "network": "cyan"}.get(rtype, "white")

        click.echo(f"    {ts:<18}  {name[:40]:<40}  "
                   f"{click.style(rtype, fg=type_color):<10}  "
                   f"{mode_str:<8}  {result_str}")

    click.echo()


# ── ui ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--config", "-c", default="configs/janitor.yaml",
              show_default=True, help="Path to config file.")
@click.option("--port", "-p", default=5000, show_default=True,
              help="Port to run the web UI on.")
def ui(config: str, port: int) -> None:
    """Launch the web dashboard."""
    from janitor.web import create_app

    app = create_app(config_path=config)
    click.echo(click.style(f"  Docker Janitor UI → http://127.0.0.1:{port}", fg="blue", bold=True))
    click.echo(click.style("  Press Ctrl+C to stop.\n", fg="bright_black"))
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    cli()
