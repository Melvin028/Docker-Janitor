"""Data models for scanned Docker resources."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ImageInfo:
    id: str                                          # full 64-char SHA256 digest
    tags: list[str]                                  # repo:tag pairs, empty = dangling
    size_bytes: int
    created_at: datetime
    in_use: bool = False                             # True if referenced by any container
    parent_id: str = ""                              # direct parent layer id
    labels: dict[str, str] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)  # child image ids
    container_ids: list[str] = field(default_factory=list)  # containers using this image

    @property
    def short_id(self) -> str:
        return self.id[:12]

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def size_human(self) -> str:
        if self.size_bytes >= 1024 ** 3:
            return f"{self.size_bytes / (1024 ** 3):.2f} GB"
        if self.size_bytes >= 1024 ** 2:
            return f"{self.size_bytes / (1024 ** 2):.2f} MB"
        return f"{self.size_bytes / 1024:.2f} KB"

    @property
    def is_dangling(self) -> bool:
        """Dangling images have no tags and are not referenced by any container."""
        return not self.tags and not self.in_use

    @property
    def display_name(self) -> str:
        return self.tags[0] if self.tags else f"<none>:{self.short_id}"

    @property
    def age_days(self) -> int:
        now = datetime.now(timezone.utc)
        created = self.created_at.replace(tzinfo=timezone.utc) if self.created_at.tzinfo is None else self.created_at
        return (now - created).days


@dataclass
class ContainerInfo:
    id: str
    name: str
    image_id: str
    image_name: str
    status: str          # e.g. "running", "exited", "paused"
    created_at: datetime
    ports: dict[str, str] = field(default_factory=dict)   # host_port → container_port
    labels: dict[str, str] = field(default_factory=dict)
    volume_names: list[str] = field(default_factory=list)
    network_names: list[str] = field(default_factory=list)

    @property
    def short_id(self) -> str:
        return self.id[:12]

    @property
    def is_running(self) -> bool:
        return self.status.lower() == "running"

    @property
    def age_days(self) -> int:
        now = datetime.now(timezone.utc)
        created = (
            self.created_at.replace(tzinfo=timezone.utc)
            if self.created_at.tzinfo is None
            else self.created_at
        )
        return (now - created).days


@dataclass
class VolumeInfo:
    name: str
    driver: str
    mount_point: str
    in_use: bool = False
    labels: dict[str, str] = field(default_factory=dict)
    scope: str = "local"


@dataclass
class NetworkInfo:
    id: str
    name: str
    driver: str
    scope: str = "local"
    in_use: bool = False
    internal: bool = False
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def short_id(self) -> str:
        return self.id[:12]


@dataclass
class DiskUsage:
    """Disk consumption breakdown from `docker system df`."""

    images_bytes: int = 0
    images_reclaimable_bytes: int = 0
    containers_bytes: int = 0
    containers_reclaimable_bytes: int = 0
    volumes_bytes: int = 0
    volumes_reclaimable_bytes: int = 0
    build_cache_bytes: int = 0
    build_cache_reclaimable_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return (
            self.images_bytes
            + self.containers_bytes
            + self.volumes_bytes
            + self.build_cache_bytes
        )

    @property
    def total_reclaimable_bytes(self) -> int:
        return (
            self.images_reclaimable_bytes
            + self.containers_reclaimable_bytes
            + self.volumes_reclaimable_bytes
            + self.build_cache_reclaimable_bytes
        )

    def _humanize(self, n: int) -> str:
        if n >= 1024 ** 3:
            return f"{n / (1024 ** 3):.2f} GB"
        if n >= 1024 ** 2:
            return f"{n / (1024 ** 2):.1f} MB"
        return f"{n / 1024:.0f} KB"

    @property
    def total_human(self) -> str:
        return self._humanize(self.total_bytes)

    @property
    def total_reclaimable_human(self) -> str:
        return self._humanize(self.total_reclaimable_bytes)

    @property
    def images_human(self) -> str:
        return self._humanize(self.images_bytes)

    @property
    def images_reclaimable_human(self) -> str:
        return self._humanize(self.images_reclaimable_bytes)

    @property
    def containers_human(self) -> str:
        return self._humanize(self.containers_bytes)

    @property
    def containers_reclaimable_human(self) -> str:
        return self._humanize(self.containers_reclaimable_bytes)

    @property
    def volumes_human(self) -> str:
        return self._humanize(self.volumes_bytes)

    @property
    def volumes_reclaimable_human(self) -> str:
        return self._humanize(self.volumes_reclaimable_bytes)

    @property
    def build_cache_human(self) -> str:
        return self._humanize(self.build_cache_bytes)

    @property
    def build_cache_reclaimable_human(self) -> str:
        return self._humanize(self.build_cache_reclaimable_bytes)


@dataclass
class ScanResult:
    images: list[ImageInfo] = field(default_factory=list)
    containers: list[ContainerInfo] = field(default_factory=list)
    volumes: list[VolumeInfo] = field(default_factory=list)
    networks: list[NetworkInfo] = field(default_factory=list)
    disk_usage: DiskUsage = field(default_factory=DiskUsage)
    scanned_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Convenience aggregates
    @property
    def dangling_images(self) -> list[ImageInfo]:
        return [i for i in self.images if i.is_dangling]

    @property
    def unused_images(self) -> list[ImageInfo]:
        return [i for i in self.images if not i.in_use]

    @property
    def stopped_containers(self) -> list[ContainerInfo]:
        return [c for c in self.containers if not c.is_running]

    @property
    def unused_volumes(self) -> list[VolumeInfo]:
        return [v for v in self.volumes if not v.in_use]

    @property
    def unused_networks(self) -> list[NetworkInfo]:
        _system = {"bridge", "host", "none"}
        return [n for n in self.networks if not n.in_use and n.name not in _system]

    @property
    def total_image_size_bytes(self) -> int:
        return sum(i.size_bytes for i in self.images)
