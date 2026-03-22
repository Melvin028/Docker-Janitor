"""Configuration loader for Docker Janitor."""

import os
import platform
from pathlib import Path
from typing import Any

import yaml


def load_config(path: str = "configs/janitor.yaml") -> dict[str, Any]:
    """Load and return configuration from a YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with config_path.open("r") as f:
        return yaml.safe_load(f) or {}


def save_config(data: dict[str, Any], path: str = "configs/janitor.yaml") -> None:
    """Write *data* back to the YAML config file, creating it if necessary."""
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def get_docker_socket() -> str:
    """Return the Docker Desktop socket path for the current platform.

    Precedence: DOCKER_HOST env var → platform default.
    """
    if env := os.environ.get("DOCKER_HOST"):
        return env

    system = platform.system()
    if system == "Windows":
        return "npipe:////./pipe/docker_engine"
    if system == "Darwin":
        user = os.getenv("USER", "unknown")
        return f"unix:///Users/{user}/.docker/run/docker.sock"
    return "unix:///var/run/docker.sock"
