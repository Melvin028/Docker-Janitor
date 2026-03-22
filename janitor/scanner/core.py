"""Scanner Core — lists images, containers, volumes, and networks; calculates usage and dependencies."""

import sys
from datetime import datetime, timezone
from typing import Any

from docker.errors import DockerException

from janitor.scanner.docker_client import get_client, get_connection_info, print_connection_summary
from janitor.scanner.images import list_images, print_images_table
from janitor.scanner.models import (
    ContainerInfo,
    DiskUsage,
    ImageInfo,
    NetworkInfo,
    ScanResult,
    VolumeInfo,
)
from janitor.utils.logger import get_logger

logger = get_logger(__name__)


def _attach_image_to_containers(
    images: list[ImageInfo], containers: list[ContainerInfo]
) -> None:
    """Populate ImageInfo.container_ids with the IDs of containers that reference each image."""
    image_map: dict[str, ImageInfo] = {}
    for img in images:
        image_map[img.id] = img
        image_map[img.short_id] = img
        for tag in img.tags:
            image_map[tag] = img

    for container in containers:
        img = (
            image_map.get(container.image_id)
            or image_map.get(container.image_id[:12])
            or image_map.get(container.image_name)
        )
        if img and container.id not in img.container_ids:
            img.container_ids.append(container.id)


def _parse_dt(raw: str | int | None) -> datetime:
    """Parse a Docker timestamp (ISO string or Unix int) into a UTC datetime."""
    if raw is None:
        return datetime.now(timezone.utc)
    if isinstance(raw, int):
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    clean = raw.split(".")[0].rstrip("Z")
    return datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)


class Scanner:
    """Connects to Docker Desktop and produces a full ScanResult."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.client = get_client((config.get("docker") or {}).get("host"))

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def scan(self) -> ScanResult:
        """Scan the Docker host and return a full ScanResult."""
        logger.info("Starting Docker resource scan...")
        containers = self._scan_containers()
        images = self._scan_images()
        _attach_image_to_containers(images, containers)
        result = ScanResult(
            images=images,
            containers=containers,
            volumes=self._scan_volumes(containers),
            networks=self._scan_networks(containers),
            disk_usage=self._scan_disk_usage(),
        )
        logger.info(
            "Scan complete — %d images (%d unused), %d containers (%d stopped), "
            "%d volumes (%d unused), %d networks (%d unused).",
            len(result.images), len(result.unused_images),
            len(result.containers), len(result.stopped_containers),
            len(result.volumes), len(result.unused_volumes),
            len(result.networks), len(result.unused_networks),
        )
        return result

    # ------------------------------------------------------------------ #
    #  Private scan methods
    # ------------------------------------------------------------------ #

    def _scan_images(self) -> list[ImageInfo]:
        """List all images from Docker Desktop including dangling ones."""
        include_dangling = (self.config.get("policy") or {}).get("include_dangling", True)
        return list_images(self.client, include_dangling=include_dangling)

    def _scan_containers(self) -> list[ContainerInfo]:
        """List all containers (running and stopped)."""
        logger.info("Scanning containers...")
        results: list[ContainerInfo] = []
        try:
            for c in self.client.containers.list(all=True):
                attrs = c.attrs or {}
                config_block = attrs.get("Config") or {}
                network_settings = attrs.get("NetworkSettings") or {}
                mounts = attrs.get("Mounts") or []

                volume_names = [
                    m["Name"] for m in mounts
                    if m.get("Type") == "volume" and m.get("Name")
                ]
                network_names = list((network_settings.get("Networks") or {}).keys())

                # Ports: map "80/tcp" → "0.0.0.0:8080"
                ports: dict[str, str] = {}
                for internal, bindings in (network_settings.get("Ports") or {}).items():
                    if bindings:
                        host = f"{bindings[0].get('HostIp', '')}:{bindings[0].get('HostPort', '')}"
                        ports[internal] = host

                results.append(ContainerInfo(
                    id=attrs.get("Id", c.id or ""),
                    name=(attrs.get("Name") or "").lstrip("/"),
                    image_id=(attrs.get("Image") or "").removeprefix("sha256:"),
                    image_name=config_block.get("Image", ""),
                    status=(attrs.get("State") or {}).get("Status", "unknown"),
                    created_at=_parse_dt(attrs.get("Created")),
                    ports=ports,
                    labels=config_block.get("Labels") or {},
                    volume_names=volume_names,
                    network_names=network_names,
                ))
        except DockerException as exc:
            logger.error("Failed to scan containers: %s", exc)
        logger.info("Found %d container(s).", len(results))
        return results

    def _scan_volumes(self, containers: list[ContainerInfo]) -> list[VolumeInfo]:
        """List all volumes and mark those mounted by any container as in-use."""
        logger.info("Scanning volumes...")
        in_use_names: set[str] = {v for c in containers for v in c.volume_names}
        results: list[VolumeInfo] = []
        try:
            for v in self.client.volumes.list():
                attrs = v.attrs or {}
                results.append(VolumeInfo(
                    name=attrs.get("Name", v.name or ""),
                    driver=attrs.get("Driver", "local"),
                    mount_point=attrs.get("Mountpoint", ""),
                    in_use=(v.name or "") in in_use_names,
                    labels=attrs.get("Labels") or {},
                    scope=attrs.get("Scope", "local"),
                ))
        except DockerException as exc:
            logger.error("Failed to scan volumes: %s", exc)
        logger.info("Found %d volume(s) (%d in use).", len(results),
                    sum(1 for v in results if v.in_use))
        return results

    def _scan_disk_usage(self) -> DiskUsage:
        """Call `docker system df` and return a structured DiskUsage breakdown."""
        logger.info("Fetching disk usage...")
        try:
            df: dict[str, Any] = self.client.df()
        except DockerException as exc:
            logger.warning("Could not fetch disk usage: %s", exc)
            return DiskUsage()

        def _sum(items: list[dict[str, Any]], key: str) -> int:
            return sum((item.get(key) or 0) for item in (items or []))

        raw_images: list[dict[str, Any]] = df.get("Images") or []
        raw_containers: list[dict[str, Any]] = df.get("Containers") or []
        raw_volumes: list[dict[str, Any]] = df.get("Volumes") or []
        raw_cache: list[dict[str, Any]] = df.get("BuildCache") or []

        stopped_containers = [
            c for c in raw_containers if (c.get("State") or "").lower() != "running"
        ]
        unused_volumes = [
            v for v in raw_volumes
            if (v.get("UsageData") or {}).get("RefCount", 1) == 0
        ]
        unused_cache = [b for b in raw_cache if not b.get("InUse")]

        return DiskUsage(
            images_bytes=_sum(raw_images, "Size"),
            images_reclaimable_bytes=_sum(
                [i for i in raw_images if not i.get("Containers")], "Size"
            ),
            containers_bytes=_sum(raw_containers, "SizeRw"),
            containers_reclaimable_bytes=_sum(stopped_containers, "SizeRw"),
            volumes_bytes=sum(
                (v.get("UsageData") or {}).get("Size", 0) or 0 for v in raw_volumes
            ),
            volumes_reclaimable_bytes=sum(
                (v.get("UsageData") or {}).get("Size", 0) or 0 for v in unused_volumes
            ),
            build_cache_bytes=_sum(raw_cache, "Size"),
            build_cache_reclaimable_bytes=_sum(unused_cache, "Size"),
        )

    def _scan_networks(self, containers: list[ContainerInfo]) -> list[NetworkInfo]:
        """List all networks and mark those connected to any container as in-use."""
        logger.info("Scanning networks...")
        in_use_names: set[str] = {n for c in containers for n in c.network_names}
        results: list[NetworkInfo] = []
        try:
            for n in self.client.networks.list():
                attrs = n.attrs or {}
                results.append(NetworkInfo(
                    id=(attrs.get("Id") or n.id or ""),
                    name=attrs.get("Name", n.name or ""),
                    driver=attrs.get("Driver", "bridge"),
                    scope=attrs.get("Scope", "local"),
                    in_use=(n.name or "") in in_use_names,
                    internal=attrs.get("Internal", False),
                    labels=attrs.get("Labels") or {},
                ))
        except DockerException as exc:
            logger.error("Failed to scan networks: %s", exc)
        logger.info("Found %d network(s) (%d in use).", len(results),
                    sum(1 for n in results if n.in_use))
        return results


# ------------------------------------------------------------------ #
#  Quick standalone runner
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    from janitor.scanner.docker_client import get_client as _get_client

    try:
        _client = _get_client()
        _info = get_connection_info(_client)
        print_connection_summary(_info)

        _scanner = Scanner(config={})
        _result = _scanner.scan()

        print_images_table(_result.images)

        # Stopped containers
        if _result.stopped_containers:
            print(f"Stopped containers ({len(_result.stopped_containers)}):")
            for c in _result.stopped_containers:
                print(f"  {c.short_id}  {c.name:<30}  {c.image_name}")
            print()

        # Unused volumes
        if _result.unused_volumes:
            print(f"Unused volumes ({len(_result.unused_volumes)}):")
            for v in _result.unused_volumes:
                print(f"  {v.name}")
            print()

        # Unused networks
        if _result.unused_networks:
            print(f"Unused networks ({len(_result.unused_networks)}):")
            for n in _result.unused_networks:
                print(f"  {n.short_id}  {n.name}  [{n.driver}]")
            print()

    except DockerException as _exc:
        print(f"\nConnection failed: {_exc}", file=sys.stderr)
        sys.exit(1)
