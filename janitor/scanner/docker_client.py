"""Docker Desktop connection client for Docker Janitor."""

import platform
import sys
from dataclasses import dataclass

import docker
from docker import DockerClient
from docker.errors import DockerException

from janitor.utils.logger import get_logger

logger = get_logger(__name__)

# Docker Desktop socket paths per platform
_SOCKET_WINDOWS = "npipe:////./pipe/docker_engine"
_SOCKET_UNIX = "unix:///var/run/docker.sock"
# Docker Desktop on macOS also exposes a user-scoped socket
_SOCKET_MACOS_USER = (
    f"unix:///Users/{__import__('os').getenv('USER', 'unknown')}/.docker/run/docker.sock"
)


@dataclass
class DockerConnectionInfo:
    base_url: str
    server_version: str
    api_version: str
    os_type: str
    architecture: str
    total_containers: int
    running_containers: int
    total_images: int


def _resolve_socket() -> str:
    """Return the correct Docker Desktop socket path for the current platform."""
    system = platform.system()
    if system == "Windows":
        return _SOCKET_WINDOWS
    if system == "Darwin":
        return _SOCKET_MACOS_USER
    return _SOCKET_UNIX


def get_client(base_url: str | None = None) -> DockerClient:
    """Return a connected Docker Desktop client.

    Tries the provided base_url first, then auto-detects the platform socket,
    and finally falls back to docker.from_env() which reads DOCKER_HOST.
    """
    candidates: list[str] = []
    if base_url:
        candidates.append(base_url)

    candidates.append(_resolve_socket())

    # On macOS also try the standard Unix socket as a fallback
    if platform.system() == "Darwin":
        candidates.append(_SOCKET_UNIX)

    last_error: Exception | None = None
    for url in candidates:
        try:
            client = docker.DockerClient(base_url=url)
            client.ping()
            logger.info("Connected to Docker Desktop via %s", url)
            return client
        except DockerException as exc:
            logger.debug("Could not connect via %s: %s", url, exc)
            last_error = exc

    # Last resort: honour DOCKER_HOST / context
    try:
        client = docker.from_env()
        client.ping()
        logger.info("Connected to Docker via environment defaults.")
        return client
    except DockerException as exc:
        last_error = exc

    logger.error("All connection attempts to Docker Desktop failed.")
    raise DockerException(
        "Cannot connect to Docker Desktop. Make sure it is running.\n"
        f"Last error: {last_error}"
    ) from last_error


def get_connection_info(client: DockerClient) -> DockerConnectionInfo:
    """Return diagnostic information about the active Docker connection."""
    try:
        info = client.info()
        version = client.version()
        return DockerConnectionInfo(
            base_url=str(client.api.base_url),
            server_version=version.get("Version", "unknown"),
            api_version=version.get("ApiVersion", "unknown"),
            os_type=info.get("OSType", "unknown"),
            architecture=info.get("Architecture", "unknown"),
            total_containers=info.get("Containers", 0),
            running_containers=info.get("ContainersRunning", 0),
            total_images=info.get("Images", 0),
        )
    except DockerException as exc:
        raise DockerException(f"Failed to retrieve Docker info: {exc}") from exc


def print_connection_summary(info: DockerConnectionInfo) -> None:
    """Print a human-readable connection summary to stdout."""
    print("\n Docker Desktop — connection established")
    print(f"   Socket       : {info.base_url}")
    print(f"   Server ver.  : {info.server_version}")
    print(f"   API ver.     : {info.api_version}")
    print(f"   OS / Arch    : {info.os_type} / {info.architecture}")
    print(f"   Containers   : {info.running_containers} running / {info.total_containers} total")
    print(f"   Images       : {info.total_images}\n")


if __name__ == "__main__":
    try:
        _client = get_client()
        _info = get_connection_info(_client)
        print_connection_summary(_info)
    except DockerException as _exc:
        print(f"\n Connection failed: {_exc}", file=sys.stderr)
        sys.exit(1)
