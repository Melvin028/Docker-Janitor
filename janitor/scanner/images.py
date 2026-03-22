"""Image scanner — lists and enriches all Docker images from Docker Desktop."""

from datetime import datetime, timezone

from docker import DockerClient
from docker.errors import DockerException
from docker.models.images import Image

from janitor.scanner.models import ImageInfo
from janitor.utils.logger import get_logger

logger = get_logger(__name__)


def _parse_created_at(raw: str | int) -> datetime:
    """Parse the image creation timestamp into a timezone-aware datetime."""
    if isinstance(raw, int):
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    # ISO 8601 string returned by newer API versions
    raw = raw.split(".")[0].rstrip("Z")
    return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)


def _build_image_info(image: Image, in_use_ids: set[str]) -> ImageInfo:
    """Convert a docker-sdk Image object into an ImageInfo datamodel."""
    attrs = image.attrs or {}
    raw_id: str = attrs.get("Id", image.id or "")
    # Strip the "sha256:" prefix for consistency
    image_id = raw_id.removeprefix("sha256:")

    tags: list[str] = image.tags or []
    size_bytes: int = attrs.get("Size", 0)
    parent_id: str = attrs.get("Parent", "").removeprefix("sha256:")
    labels: dict[str, str] = attrs.get("Labels") or {}
    created_at = _parse_created_at(attrs.get("Created", 0))

    return ImageInfo(
        id=image_id,
        tags=sorted(tags),
        size_bytes=size_bytes,
        created_at=created_at,
        in_use=image_id in in_use_ids or raw_id in in_use_ids,
        parent_id=parent_id,
        labels=labels,
    )


def _get_in_use_image_ids(client: DockerClient) -> set[str]:
    """Return the set of image IDs currently referenced by any container."""
    in_use: set[str] = set()
    try:
        for container in client.containers.list(all=True):
            attrs = container.attrs or {}
            image_id: str = attrs.get("Image", "") or ""
            config_image: str = (attrs.get("Config") or {}).get("Image", "") or ""
            if image_id:
                in_use.add(image_id.removeprefix("sha256:"))
            if config_image:
                in_use.add(config_image)
    except DockerException as exc:
        logger.warning("Could not determine in-use images: %s", exc)
    return in_use


def _attach_dependencies(images: list[ImageInfo]) -> None:
    """Populate the dependencies list: map each parent_id → child image ids."""
    parent_map: dict[str, list[str]] = {}
    for img in images:
        if img.parent_id:
            parent_map.setdefault(img.parent_id, []).append(img.id)

    for img in images:
        img.dependencies = parent_map.get(img.id, [])


def list_images(client: DockerClient, include_dangling: bool = True) -> list[ImageInfo]:
    """Return all Docker images from Docker Desktop as a list of ImageInfo objects.

    Args:
        client:           Connected DockerClient instance.
        include_dangling: When True, untagged (dangling) images are included.

    Returns:
        Sorted list of ImageInfo — tagged images first, then dangling, both
        sorted by creation date descending (newest first).
    """
    logger.info("Scanning images (include_dangling=%s)...", include_dangling)

    try:
        raw_images: list[Image] = client.images.list(all=include_dangling)
    except DockerException as exc:
        logger.error("Failed to list images: %s", exc)
        raise

    in_use_ids = _get_in_use_image_ids(client)
    images = [_build_image_info(img, in_use_ids) for img in raw_images]
    _attach_dependencies(images)

    # Sort: tagged first, then by age (newest → oldest)
    images.sort(key=lambda i: (i.is_dangling, -i.created_at.timestamp()))

    logger.info("Found %d image(s) (%d in use, %d dangling).",
                len(images),
                sum(1 for i in images if i.in_use),
                sum(1 for i in images if i.is_dangling))
    return images


def print_images_table(images: list[ImageInfo]) -> None:
    """Print a formatted table of images to stdout."""
    if not images:
        print("No images found.")
        return

    col_name  = 45
    col_id    = 14
    col_size  = 10
    col_age   = 8
    col_use   = 8

    header = (
        f"{'REPOSITORY:TAG':<{col_name}}"
        f"{'IMAGE ID':<{col_id}}"
        f"{'SIZE':>{col_size}}"
        f"{'AGE (d)':>{col_age}}"
        f"{'IN USE':>{col_use}}"
    )
    separator = "-" * len(header)

    print(f"\n{'Docker Desktop — Images':^{len(header)}}")
    print(separator)
    print(header)
    print(separator)

    for img in images:
        name  = img.display_name[:col_name - 1]
        print(
            f"{name:<{col_name}}"
            f"{img.short_id:<{col_id}}"
            f"{img.size_human:>{col_size}}"
            f"{img.age_days:>{col_age}}"
            f"{'yes' if img.in_use else 'no':>{col_use}}"
        )

    print(separator)
    total_mb = round(sum(i.size_bytes for i in images) / (1024 ** 2), 2)
    print(f"  {len(images)} image(s)  |  Total size: {total_mb} MB")
    dangling = sum(1 for i in images if i.is_dangling)
    if dangling:
        print(f"  {dangling} dangling image(s) can be pruned.")
    print()
