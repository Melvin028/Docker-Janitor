"""Route handlers for the Docker Janitor web UI."""

from __future__ import annotations

from docker.errors import DockerException
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from janitor.config import load_config, save_config
from janitor.scanner.core import Scanner
from janitor.scanner.models import ScanResult

bp = Blueprint("main", __name__)

# Module-level store for the last scan result.
# This is intentionally simple: Docker Janitor is a single-user local tool.
_last_scan: ScanResult | None = None


def _load_config() -> dict:
    """Load config, falling back to the example file if the main one is missing."""
    primary = current_app.config["JANITOR_CONFIG_PATH"]
    for path in (primary, "configs/janitor.example.yaml"):
        try:
            return load_config(path)
        except FileNotFoundError:
            continue
    return {}


# ------------------------------------------------------------------ #
#  Routes
# ------------------------------------------------------------------ #


@bp.get("/")
def dashboard() -> str:
    from collections import defaultdict
    from datetime import datetime, timedelta, timezone

    from janitor.history.store import compute_trend, read_history

    history = read_history(limit=30)
    trend   = compute_trend(history)

    heatmap_cells: list[dict] = []
    duplicate_groups: list = []

    if _last_scan:
        # ── Image Age Heatmap (last 52 weeks) ──────────────────────────
        now = datetime.now(timezone.utc)

        # Index images by ISO year-week key
        week_image_map: dict[tuple, list] = defaultdict(list)
        for img in _last_scan.images:
            created = img.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            iso = created.isocalendar()
            week_image_map[(iso[0], iso[1])].append({
                "display_name": img.display_name,
                "short_id":     img.short_id,
                "size_human":   img.size_human,
                "size_bytes":   img.size_bytes,
                "tags":         img.tags,
                "in_use":       img.in_use,
                "is_dangling":  img.is_dangling,
                "age_days":     img.age_days,
                "created_fmt":  created.strftime("%b %d, %Y").replace(" 0", " "),
            })

        months_seen: set[tuple] = set()
        for i in range(51, -1, -1):              # oldest → newest
            d = now - timedelta(weeks=i)
            iso = d.isocalendar()
            yw = (iso[0], iso[1])
            monday = d - timedelta(days=d.weekday())
            month_key = (monday.year, monday.month)
            month_label = monday.strftime("%b") if month_key not in months_seen else ""
            months_seen.add(month_key)
            images_this_week = week_image_map.get(yw, [])
            count = len(images_this_week)
            level = (0 if count == 0 else
                     1 if count <= 2 else
                     2 if count <= 5 else
                     3 if count <= 10 else 4)
            heatmap_cells.append({
                "week":        f"{yw[0]}-W{yw[1]:02d}",
                "count":       count,
                "label":       monday.strftime("%b %d, %Y").replace(" 0", " "),
                "level":       level,
                "month_label": month_label,
                "images":      images_this_week,   # full image list for click-through
            })

        # ── Duplicate Image Detector ────────────────────────────────────
        # Images with >1 tag share the same SHA — multiple aliases, one copy on disk.
        duplicate_groups = [img for img in _last_scan.images if len(img.tags) > 1]

    # ── Smart Cleanup Recommendations ──────────────────────────────────────
    recommendations: list[dict] = []
    if _last_scan:
        def _rec_humanize(n: int) -> str:
            if n >= 1024 ** 3:
                return f"{n / 1024 ** 3:.2f} GB"
            if n >= 1024 ** 2:
                return f"{n / 1024 ** 2:.1f} MB"
            return f"{n / 1024:.0f} KB"

        dangling = _last_scan.dangling_images
        if dangling:
            b = sum(i.size_bytes for i in dangling)
            recommendations.append({
                "type": "dangling",
                "title": f"Remove {len(dangling)} dangling image{'s' if len(dangling) != 1 else ''}",
                "description": "Untagged, unreferenced images — safe to delete immediately.",
                "count": len(dangling),
                "bytes": b,
                "bytes_human": _rec_humanize(b),
            })

        stale = [i for i in _last_scan.images if not i.in_use and not i.is_dangling and i.age_days > 30]
        if stale:
            b = sum(i.size_bytes for i in stale)
            recommendations.append({
                "type": "stale_images",
                "title": f"Remove {len(stale)} unused image{'s' if len(stale) != 1 else ''} older than 30 days",
                "description": "Not referenced by any container and untouched for over a month.",
                "count": len(stale),
                "bytes": b,
                "bytes_human": _rec_humanize(b),
            })

        stopped = _last_scan.stopped_containers
        if stopped:
            recommendations.append({
                "type": "containers",
                "title": f"Remove {len(stopped)} stopped container{'s' if len(stopped) != 1 else ''}",
                "description": "Exited containers accumulate filesystem layers and clutter the environment.",
                "count": len(stopped),
                "bytes": 0,
                "bytes_human": None,
            })

        orphaned = _last_scan.unused_volumes
        if orphaned:
            b = _last_scan.disk_usage.volumes_reclaimable_bytes
            recommendations.append({
                "type": "volumes",
                "title": f"Remove {len(orphaned)} orphaned volume{'s' if len(orphaned) != 1 else ''}",
                "description": "Volumes not mounted by any active container.",
                "count": len(orphaned),
                "bytes": b,
                "bytes_human": _rec_humanize(b) if b else None,
            })

        cache_b = _last_scan.disk_usage.build_cache_reclaimable_bytes
        if cache_b > 50 * 1024 * 1024:
            recommendations.append({
                "type": "cache",
                "title": "Prune build cache",
                "description": f"{_rec_humanize(cache_b)} of reclaimable build cache.",
                "count": 0,
                "bytes": cache_b,
                "bytes_human": _rec_humanize(cache_b),
            })

        recommendations.sort(key=lambda r: r["bytes"], reverse=True)

    return render_template(
        "dashboard.html",
        result=_last_scan,
        history=history,
        trend=trend,
        heatmap_cells=heatmap_cells,
        duplicate_groups=duplicate_groups,
        recommendations=recommendations,
    )


@bp.get("/api/scan-history")
def api_scan_history():
    """Return the last 90 scan history entries as JSON (for Chart.js)."""
    import json as _json
    from janitor.history.store import read_history
    history = read_history(limit=90)
    response = current_app.response_class(
        _json.dumps(history, default=str),
        mimetype="application/json",
    )
    return response


@bp.post("/scan")
def do_scan():
    global _last_scan
    try:
        cfg = _load_config()
        _last_scan = Scanner(cfg).scan()
        # Persist a lightweight snapshot for the trend chart
        try:
            from janitor.history.store import append_scan
            append_scan(_last_scan)
        except Exception:  # noqa: BLE001
            pass
        flash("Scan complete.", "success")
    except DockerException as exc:
        flash(f"Could not connect to Docker: {exc}", "error")
    except Exception as exc:  # noqa: BLE001
        flash(f"Scan failed: {exc}", "error")
    return redirect(url_for("main.dashboard"))


@bp.get("/api/image/<image_id>/layers")
def api_image_layers(image_id: str):
    """Return the layer history for an image with cross-image sharing info."""
    import json as _json
    from janitor.scanner.docker_client import get_client

    cfg = _load_config()
    try:
        client = get_client((cfg.get("docker") or {}).get("host"))
    except Exception as exc:  # noqa: BLE001
        return current_app.response_class(
            _json.dumps({"error": str(exc)}), mimetype="application/json", status=500
        )

    # Build a map: layer_id → list of image display names (across all known images)
    layer_owners: dict[str, list[str]] = {}
    if _last_scan:
        for img in _last_scan.images:
            try:
                hist = client.api.history(img.id)
                for layer in hist:
                    lid = (layer.get("Id") or "").removeprefix("sha256:")
                    if lid and lid != "<missing>":
                        layer_owners.setdefault(lid, [])
                        label = img.tags[0] if img.tags else img.id[:12]
                        if label not in layer_owners[lid]:
                            layer_owners[lid].append(label)
            except Exception:  # noqa: BLE001
                pass

    # Fetch layers for the requested image
    try:
        history = client.api.history(image_id)
    except Exception as exc:  # noqa: BLE001
        return current_app.response_class(
            _json.dumps({"error": str(exc)}), mimetype="application/json", status=404
        )

    layers = []
    for layer in history:
        lid = (layer.get("Id") or "").removeprefix("sha256:")
        cmd = (layer.get("CreatedBy") or "").strip()
        # Clean up the command: strip common /bin/sh -c prefixes
        for prefix in ("/bin/sh -c #(nop) ", "/bin/sh -c "):
            if cmd.startswith(prefix):
                cmd = cmd[len(prefix):]
                break
        size = layer.get("Size", 0) or 0
        created = layer.get("Created", 0) or 0
        owners = layer_owners.get(lid, []) if lid and lid != "<missing>" else []
        layers.append({
            "id":        lid[:12] if lid else "<missing>",
            "full_id":   lid,
            "command":   cmd,
            "size":      size,
            "size_human": _humanize_bytes(size) if size else "0 B",
            "created":   created,
            "shared_by": owners,
            "shared":    len(owners) > 1,
        })

    payload = _json.dumps({"layers": layers}, default=str)
    return current_app.response_class(payload, mimetype="application/json")


@bp.get("/images")
def images() -> str:
    containers_by_id = (
        {c.id: c for c in _last_scan.containers} if _last_scan else {}
    )
    return render_template("images.html", result=_last_scan, containers_by_id=containers_by_id)


@bp.get("/containers")
def containers() -> str:
    return render_template("containers.html", result=_last_scan)


@bp.get("/volumes")
def volumes() -> str:
    return render_template("volumes.html", result=_last_scan)


@bp.get("/networks")
def networks() -> str:
    return render_template("networks.html", result=_last_scan)


@bp.get("/stats")
def stats() -> str:
    return render_template("stats.html", result=_last_scan)


@bp.get("/projects")
def projects() -> str:
    """Group containers, images, and volumes by Docker Compose project label."""
    if not _last_scan:
        return render_template("projects.html", result=None, project_groups=[], non_compose_count=0)

    from collections import defaultdict

    PROJ_LABEL    = "com.docker.compose.project"
    SERVICE_LABEL = "com.docker.compose.service"

    image_map  = {img.id:   img for img in _last_scan.images}
    volume_map = {v.name:   v   for v   in _last_scan.volumes}

    grouped: dict[str, list] = defaultdict(list)
    non_compose = 0
    for c in _last_scan.containers:
        proj = (c.labels or {}).get(PROJ_LABEL)
        if proj:
            grouped[proj].append(c)
        else:
            non_compose += 1

    project_groups = []
    for proj_name, ctrs in sorted(grouped.items()):
        img_ids = {c.image_id for c in ctrs}
        imgs    = [image_map[iid] for iid in img_ids if iid in image_map]

        vol_names: set[str] = set()
        for c in ctrs:
            vol_names.update(c.volume_names or [])
        vols = [volume_map[vn] for vn in vol_names if vn in volume_map]

        running = sum(1 for c in ctrs if c.is_running)
        img_bytes = sum(img.size_bytes for img in imgs)

        services = sorted({(c.labels or {}).get(SERVICE_LABEL, c.name) for c in ctrs})

        project_groups.append({
            "name":             proj_name,
            "containers":       sorted(ctrs, key=lambda c: c.name),
            "images":           sorted(imgs, key=lambda i: i.size_bytes, reverse=True),
            "volumes":          sorted(vols, key=lambda v: v.name),
            "services":         services,
            "running_count":    running,
            "stopped_count":    len(ctrs) - running,
            "all_stopped":      running == 0,
            "total_image_bytes": img_bytes,
            "total_image_human": _humanize_bytes(img_bytes) if img_bytes else "0 B",
        })

    return render_template(
        "projects.html",
        result=_last_scan,
        project_groups=project_groups,
        non_compose_count=non_compose,
    )


@bp.post("/projects/<project_name>/remove")
def remove_project(project_name: str):
    """Remove all stopped containers and labeled volumes for a Compose project."""
    from janitor.audit.logger import append_entry, make_entry
    from janitor.scanner.docker_client import get_client

    cfg = _load_config()
    try:
        client = get_client((cfg.get("docker") or {}).get("host"))
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not connect to Docker: {exc}", "error")
        return redirect(url_for("main.projects"))

    label_filter = f"com.docker.compose.project={project_name}"

    try:
        containers = client.containers.list(all=True, filters={"label": label_filter})
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not list containers: {exc}", "error")
        return redirect(url_for("main.projects"))

    running = [c for c in containers if c.status == "running"]
    if running:
        names = ", ".join(c.name.lstrip("/") for c in running)
        flash(
            f"Cannot remove '{project_name}': {len(running)} container(s) still running "
            f"({names}). Stop them first.",
            "error",
        )
        return redirect(url_for("main.projects"))

    removed, errors = 0, []

    for c in containers:
        disp = c.name.lstrip("/")
        try:
            c.remove()
            removed += 1
            append_entry(make_entry(
                resource_id=c.id, resource_type="container", display_name=disp,
                size_bytes=0, action="remove", dry_run=False, success=True,
                message=f"Removed as part of Compose project '{project_name}'",
                reason=f"compose_project_cleanup",
            ))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{disp}: {exc}")
            append_entry(make_entry(
                resource_id=c.id, resource_type="container", display_name=disp,
                size_bytes=0, action="remove", dry_run=False, success=False,
                message=str(exc), reason="compose_project_cleanup",
            ))

    try:
        for v in client.volumes.list(filters={"label": label_filter}):
            try:
                v.remove()
                removed += 1
                append_entry(make_entry(
                    resource_id=v.name, resource_type="volume", display_name=v.name,
                    size_bytes=0, action="remove", dry_run=False, success=True,
                    message=f"Removed as part of Compose project '{project_name}'",
                    reason="compose_project_cleanup",
                ))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"volume {v.name}: {exc}")
    except Exception:  # noqa: BLE001
        pass

    if errors:
        flash(f"Partial removal of '{project_name}': {removed} resource(s) removed, "
              f"{len(errors)} error(s): {'; '.join(errors)}", "warning")
    else:
        flash(f"Project '{project_name}' removed: {removed} resource(s) cleaned up.", "success")

    return redirect(url_for("main.projects"))


@bp.get("/api/container-stats")
def api_container_stats():
    """Return live CPU, memory, and network stats for all running containers.

    Uses one_shot=True (single sample, no 1-second wait) and parallel threads
    so the response arrives in ~1 second regardless of container count.
    """
    import json as _json
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from janitor.scanner.docker_client import get_client

    cfg = _load_config()
    try:
        client = get_client((cfg.get("docker") or {}).get("host"))
    except Exception as exc:  # noqa: BLE001
        return current_app.response_class(
            _json.dumps({"error": str(exc)}), mimetype="application/json", status=500
        )

    def _parse_stats(c) -> dict | None:  # type: ignore[return]
        try:
            # one_shot=True → single reading, no blocking wait period
            s = c.stats(stream=False, decode=None)

            # CPU %
            cpu_delta    = (s["cpu_stats"]["cpu_usage"]["total_usage"]
                            - s["precpu_stats"]["cpu_usage"]["total_usage"])
            system_delta = (s["cpu_stats"].get("system_cpu_usage", 0)
                            - s["precpu_stats"].get("system_cpu_usage", 0))
            num_cpus     = (s["cpu_stats"].get("online_cpus")
                            or len(s["cpu_stats"]["cpu_usage"].get("percpu_usage") or [1]))
            cpu_pct = (cpu_delta / system_delta) * num_cpus * 100 if system_delta > 0 else 0.0

            # Memory
            mem_stats = s.get("memory_stats", {})
            mem_usage = mem_stats.get("usage", 0)
            mem_limit = mem_stats.get("limit", 1)
            mem_pct   = (mem_usage / mem_limit * 100) if mem_limit > 0 else 0.0

            # Network I/O
            networks_io = s.get("networks", {})
            rx_bytes = sum(v.get("rx_bytes", 0) for v in networks_io.values())
            tx_bytes = sum(v.get("tx_bytes", 0) for v in networks_io.values())

            # Block I/O
            blkio       = (s.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []
            read_bytes  = sum(x["value"] for x in blkio if x.get("op") == "Read")
            write_bytes = sum(x["value"] for x in blkio if x.get("op") == "Write")

            tags = c.image.tags
            return {
                "id":              c.id[:12],
                "name":            c.name.lstrip("/"),
                "image":           tags[0] if tags else c.image.id[:12],
                "status":          c.status,
                "cpu_pct":         round(cpu_pct, 2),
                "mem_usage":       mem_usage,
                "mem_limit":       mem_limit,
                "mem_usage_human": _humanize_bytes(mem_usage),
                "mem_limit_human": _humanize_bytes(mem_limit),
                "mem_pct":         round(mem_pct, 2),
                "rx_human":        _humanize_bytes(rx_bytes),
                "tx_human":        _humanize_bytes(tx_bytes),
                "read_human":      _humanize_bytes(read_bytes),
                "write_human":     _humanize_bytes(write_bytes),
            }
        except Exception:  # noqa: BLE001
            return None

    stats_list: list[dict] = []
    try:
        containers = client.containers.list()   # running containers only
        if containers:
            with ThreadPoolExecutor(max_workers=min(len(containers), 16)) as pool:
                futures = {pool.submit(_parse_stats, c): c for c in containers}
                for fut in as_completed(futures, timeout=10):
                    result = fut.result()
                    if result:
                        stats_list.append(result)
        stats_list.sort(key=lambda x: x["name"])
    except Exception as exc:  # noqa: BLE001
        return current_app.response_class(
            _json.dumps({"error": str(exc)}), mimetype="application/json", status=500
        )

    payload = _json.dumps({"stats": stats_list, "ts": _time.time()})
    return current_app.response_class(payload, mimetype="application/json")


@bp.get("/graph")
def dependency_graph() -> str:
    """Image → container dependency view (grouped card layout)."""
    if not _last_scan:
        return render_template(
            "graph.html", result=None, groups=[],
            hidden_unused=0, hidden_dangling=0,
        )

    image_map = {img.id: img for img in _last_scan.images}

    # Group containers by the image they use
    from collections import defaultdict
    containers_by_image: dict[str, list] = defaultdict(list)
    for c in _last_scan.containers:
        if c.image_id in image_map:
            containers_by_image[c.image_id].append(c)

    # Build groups — only images that have at least one container
    groups: list[dict] = []
    connected_ids: set[str] = set()
    for img in sorted(_last_scan.images, key=lambda i: i.size_bytes, reverse=True):
        ctrs = containers_by_image.get(img.id, [])
        if not ctrs:
            continue
        connected_ids.add(img.id)
        groups.append({
            "image": {
                "id":           img.id,
                "short_id":     img.short_id,
                "display_name": img.display_name,
                "size_human":   img.size_human,
                "tags":         img.tags,
            },
            "containers": [
                {
                    "id":     c.id,
                    "name":   c.name,
                    "status": c.status,
                }
                for c in sorted(ctrs, key=lambda c: c.status)
            ],
        })

    hidden_unused   = sum(1 for img in _last_scan.images
                         if img.id not in connected_ids and not img.is_dangling)
    hidden_dangling = len(_last_scan.dangling_images)

    return render_template(
        "graph.html",
        result=_last_scan,
        groups=groups,
        hidden_unused=hidden_unused,
        hidden_dangling=hidden_dangling,
    )


# ------------------------------------------------------------------ #
#  Management routes
# ------------------------------------------------------------------ #


@bp.get("/policy")
def policy() -> str:
    from janitor.policy.engine import PolicyEngine
    cfg = _load_config()
    policy_result = PolicyEngine(cfg).evaluate(_last_scan) if _last_scan else None
    return render_template("policy.html", result=_last_scan, policy_result=policy_result, cfg=cfg)


def _humanize_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / (1024 ** 3):.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / (1024 ** 2):.1f} MB"
    return f"{n / 1024:.0f} KB"


@bp.get("/cleanup")
def cleanup() -> str:
    from janitor.policy.engine import PolicyEngine
    from janitor.scanner.docker_client import get_client

    if not _last_scan:
        return render_template(
            "cleanup.html", result=None, preview_items=[],
            cache_count=0, cache_bytes=0, cache_bytes_human="0 B",
        )

    cfg = _load_config()
    policy_result = PolicyEngine(cfg).evaluate(_last_scan)

    image_map     = {img.id:  img for img in _last_scan.images}
    container_map = {c.id:    c   for c   in _last_scan.containers}
    volume_map    = {v.name:  v   for v   in _last_scan.volumes}

    preview_items = []
    for d in policy_result.decisions:
        if not d.safe_to_delete:
            continue

        if d.resource_type == "image":
            img = image_map.get(d.resource_id)
            preview_items.append({
                "resource_id":   d.resource_id,
                "resource_type": "image",
                "display_name":  img.display_name if img else d.resource_id[:12],
                "size_bytes":    img.size_bytes if img else 0,
                "size_human":    img.size_human if img else "—",
                "age_days":      img.age_days if img else 0,
                "reason":        d.reason,
            })

        elif d.resource_type == "container":
            c = container_map.get(d.resource_id)
            preview_items.append({
                "resource_id":   d.resource_id,
                "resource_type": "container",
                "display_name":  c.name if c else d.resource_id[:12],
                "size_bytes":    0,
                "size_human":    "—",
                "age_days":      c.age_days if c else 0,
                "reason":        d.reason,
            })

        elif d.resource_type == "volume":
            v = volume_map.get(d.resource_id)
            preview_items.append({
                "resource_id":   d.resource_id,
                "resource_type": "volume",
                "display_name":  v.name if v else d.resource_id,
                "size_bytes":    0,
                "size_human":    "—",
                "age_days":      0,
                "reason":        d.reason,
            })

    # Fetch unused build-cache metrics (re-uses the existing Docker connection).
    cache_count, cache_bytes = 0, 0
    try:
        client = get_client((cfg.get("docker") or {}).get("host"))
        df = client.df()
        unused = [c for c in (df.get("BuildCache") or []) if not c.get("InUse")]
        cache_count = len(unused)
        cache_bytes = sum(c.get("Size", 0) for c in unused)
    except Exception:  # noqa: BLE001
        pass

    return render_template(
        "cleanup.html",
        result=_last_scan,
        preview_items=preview_items,
        cache_count=cache_count,
        cache_bytes=cache_bytes,
        cache_bytes_human=_humanize_bytes(cache_bytes) if cache_bytes else "0 B",
    )


@bp.post("/cleanup/execute")
def cleanup_execute():
    from janitor.policy.engine import PolicyEngine, PolicyResult
    from janitor.cleanup.engine import CleanupEngine

    if not _last_scan:
        flash("No scan data. Run a scan first.", "error")
        return redirect(url_for("main.cleanup"))

    selected_ids = set(request.form.getlist("resource_ids"))
    if not selected_ids:
        flash("No resources selected.", "error")
        return redirect(url_for("main.cleanup"))

    try:
        cfg = _load_config()
        policy_result = PolicyEngine(cfg).evaluate(_last_scan)

        # Only execute on the explicitly selected resources
        selected_decisions = [
            d for d in policy_result.decisions
            if d.resource_id in selected_ids and d.safe_to_delete
        ]
        filtered_result = PolicyResult(
            decisions=selected_decisions,
            scan_result=policy_result.scan_result,
        )

        result = CleanupEngine(cfg, dry_run=False).execute(filtered_result)

        # Build notification payload from successful deletions
        try:
            from janitor.notifier import build_payload, send_notifications
            image_map     = {img.id:  img for img in _last_scan.images}
            container_map = {c.id:    c   for c   in _last_scan.containers}
            volume_map    = {v.name:  v   for v   in _last_scan.volumes}
            notif_items = []
            for d in selected_decisions:
                item: dict = {
                    "name":       d.resource_id[:12],
                    "type":       d.resource_type,
                    "size_bytes": 0,
                    "size_human": "—",
                }
                if d.resource_type == "image" and d.resource_id in image_map:
                    img = image_map[d.resource_id]
                    item["name"]       = img.display_name
                    item["size_bytes"] = img.size_bytes
                    item["size_human"] = img.size_human
                elif d.resource_type == "container" and d.resource_id in container_map:
                    item["name"] = container_map[d.resource_id].name
                elif d.resource_type == "volume" and d.resource_id in volume_map:
                    item["name"] = volume_map[d.resource_id].name
                notif_items.append(item)

            notif_payload = build_payload(notif_items, dry_run=False)
            send_notifications(cfg, notif_payload)
        except Exception:  # noqa: BLE001
            pass

        flash(
            f"Cleanup complete. {result.deleted_count} resource(s) removed.",
            "success",
        )
    except Exception as exc:  # noqa: BLE001
        flash(f"Cleanup failed: {exc}", "error")

    return redirect(url_for("main.audit"))


@bp.post("/cleanup/prune-cache")
def cleanup_prune_cache():
    """Prune all unused Docker build-cache layers."""
    from datetime import datetime, timezone
    from janitor.audit.logger import append_entry
    from janitor.scanner.docker_client import get_client

    try:
        cfg = _load_config()
        client = get_client((cfg.get("docker") or {}).get("host"))
        result = client.api.prune_builds()
        freed = result.get("SpaceReclaimed", 0)

        append_entry({
            "timestamp":     datetime.now(timezone.utc).isoformat(),
            "resource_id":   "build-cache",
            "resource_type": "build_cache",
            "display_name":  "Build cache",
            "size_bytes":    freed,
            "action":        "prune",
            "dry_run":       False,
            "success":       True,
            "message":       f"Pruned build cache, freed {freed} bytes",
            "reason":        "Manual prune from UI",
            "tags":          [],
            "pull_commands": [],
            "recoverable":   False,
        })

        flash(
            f"Build cache pruned — {_humanize_bytes(freed)} freed." if freed
            else "Build cache pruned (nothing to free).",
            "success",
        )
    except Exception as exc:  # noqa: BLE001
        flash(f"Failed to prune build cache: {exc}", "error")

    return redirect(url_for("main.cleanup"))


@bp.get("/audit")
def audit() -> str:
    from janitor.audit.logger import read_entries
    entries = read_entries(limit=500)
    total_freed = sum(
        e.get("size_bytes", 0)
        for e in entries
        if e.get("success") and not e.get("dry_run")
    )
    return render_template("audit.html", entries=entries, total_freed=total_freed)


@bp.get("/audit/export")
def audit_export():
    """Download the audit log as CSV or JSON."""
    import csv
    import io
    import json as _json
    from janitor.audit.logger import read_entries

    fmt = request.args.get("fmt", "json").lower()
    entries = read_entries(limit=100_000)

    if fmt == "csv":
        fields = [
            "timestamp", "resource_type", "display_name",
            "size_bytes", "action", "dry_run", "success", "reason", "message",
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for e in entries:
            writer.writerow({f: e.get(f, "") for f in fields})
        response = current_app.response_class(buf.getvalue(), mimetype="text/csv")
        response.headers["Content-Disposition"] = "attachment; filename=audit.csv"
        return response

    data = _json.dumps(entries, indent=2, default=str)
    response = current_app.response_class(data, mimetype="application/json")
    response.headers["Content-Disposition"] = "attachment; filename=audit.json"
    return response


@bp.post("/audit/clear")
def audit_clear():
    from janitor.audit.logger import clear_log
    try:
        clear_log()
        flash("Audit log cleared.", "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not clear log: {exc}", "error")
    return redirect(url_for("main.audit"))


@bp.post("/audit/recover")
def audit_recover():
    """Pull a previously deleted image back from its registry source."""
    from datetime import datetime, timezone
    from janitor.audit.logger import append_entry
    from janitor.scanner.docker_client import get_client

    tag = request.form.get("tag", "").strip()
    if not tag:
        flash("No tag provided for recovery.", "error")
        return redirect(url_for("main.audit"))

    try:
        cfg = _load_config()
        client = get_client((cfg.get("docker") or {}).get("host"))

        # Split "nginx:1.25.3" → repository + tag; default tag to "latest"
        repo, _, tag_part = tag.partition(":")
        tag_part = tag_part or "latest"

        client.images.pull(repo, tag=tag_part)

        append_entry({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "resource_id": "",
            "resource_type": "image",
            "display_name": tag,
            "size_bytes": 0,
            "action": "recover",
            "dry_run": False,
            "success": True,
            "message": f"Pulled {tag} successfully",
            "reason": "Manual recovery from audit log",
            "tags": [tag],
            "pull_commands": [f"docker pull {tag}"],
            "recoverable": False,
        })

        flash(f"Recovered: {tag} pulled successfully.", "success")

    except Exception as exc:  # noqa: BLE001
        flash(f"Recovery failed for '{tag}': {exc}", "error")

    return redirect(url_for("main.audit"))


@bp.get("/settings")
def settings() -> str:
    cfg = _load_config()
    return render_template("settings.html", cfg=cfg)


@bp.post("/settings/save")
def settings_save():
    """Persist the policy form fields back to janitor.yaml."""
    try:
        cfg = _load_config()

        # Parse form values with safe type coercion
        retention_raw = request.form.get("retention_days", "").strip()
        min_versions_raw = request.form.get("min_versions", "").strip()
        keep_patterns_raw = request.form.get("keep_patterns", "").strip()

        policy: dict = cfg.get("policy", {})

        policy["retention_days"] = int(retention_raw) if retention_raw.isdigit() else None
        # Store 0 explicitly so the engine knows the guard is disabled
        policy["min_versions"] = int(min_versions_raw) if min_versions_raw.isdigit() else 0
        policy["protect_running"] = request.form.get("protect_running") == "on"
        policy["protect_named"] = request.form.get("protect_named") == "on"

        ctr_raw = request.form.get("container_retention_days", "0").strip()
        policy["container_retention_days"] = int(ctr_raw) if ctr_raw.isdigit() else 0
        policy["cleanup_orphaned_volumes"] = request.form.get("cleanup_orphaned_volumes") == "on"

        # keep_patterns: one pattern per line, skip blank lines
        if keep_patterns_raw:
            policy["keep_patterns"] = [
                p.strip() for p in keep_patterns_raw.splitlines() if p.strip()
            ]
        else:
            policy["keep_patterns"] = []

        # Remove None values so YAML stays clean
        policy = {k: v for k, v in policy.items() if v is not None}

        cfg["policy"] = policy

        # Notification toggles (env-var credentials are never stored here)
        notif: dict = cfg.get("notifications") or {}
        for key in ("cli", "slack", "webhook", "email"):
            notif.setdefault(key, {})["enabled"] = request.form.get(f"notif_{key}") == "on"
        cfg["notifications"] = notif

        config_path = current_app.config["JANITOR_CONFIG_PATH"]
        save_config(cfg, config_path)
        flash("Settings saved.", "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not save settings: {exc}", "error")

    return redirect(url_for("main.settings"))
