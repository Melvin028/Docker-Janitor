"""Cleanup Engine — performs dry-run or live deletion of Docker resources and logs all actions."""

from dataclasses import dataclass, field
from typing import Any

from docker.errors import ImageNotFound, NotFound

from janitor.audit.logger import append_entry, make_entry
from janitor.policy.engine import PolicyResult, ResourceDecision
from janitor.scanner.docker_client import get_client
from janitor.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CleanupAction:
    resource_id: str
    resource_type: str
    action: str
    dry_run: bool
    success: bool
    message: str


@dataclass
class CleanupResult:
    actions: list[CleanupAction] = field(default_factory=list)
    dry_run: bool = True

    @property
    def deleted_count(self) -> int:
        return sum(1 for a in self.actions if a.success and not a.dry_run)

    @property
    def would_delete_count(self) -> int:
        return sum(1 for a in self.actions if a.dry_run)


class CleanupEngine:
    """Executes deletion decisions produced by the PolicyEngine."""

    def __init__(self, config: dict[str, Any], dry_run: bool = True) -> None:
        self.config = config
        self.dry_run = dry_run
        self.client = get_client((config.get("docker") or {}).get("host"))

    def execute(self, policy_result: PolicyResult) -> CleanupResult:
        """Execute or simulate deletions based on policy decisions."""
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        logger.info("Cleanup Engine running in %s mode.", mode)
        actions: list[CleanupAction] = []
        for decision in policy_result.decisions:
            if decision.safe_to_delete:
                action = self._delete_resource(decision)
                actions.append(action)
        return CleanupResult(actions=actions, dry_run=self.dry_run)

    def _delete_resource(self, decision: ResourceDecision) -> CleanupAction:
        """Delete (or simulate deleting) one resource and write an audit entry."""
        resource_type = decision.resource_type
        resource_id = decision.resource_id
        display_name = resource_id[:12]
        size_bytes = 0
        tags: list[str] = []
        success = False
        message = ""

        if self.dry_run:
            display_name, size_bytes, tags = self._resolve_display(resource_type, resource_id)
            success = True
            message = "Would delete (dry-run)"
            logger.info("[DRY-RUN] Would delete %s %s", resource_type, display_name)
        else:
            # Capture metadata (including tags) BEFORE deletion — once gone the
            # Docker API can no longer return them.
            display_name, size_bytes, tags = self._resolve_display(resource_type, resource_id)
            try:
                self._do_delete(resource_type, resource_id)
                success = True
                message = "Deleted successfully"
                logger.info("Deleted %s %s", resource_type, display_name)
            except (ImageNotFound, NotFound):
                success = True  # already gone — treat as success
                message = "Already removed"
                logger.warning("%s %s no longer exists, skipping.", resource_type, display_name)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                logger.error("Failed to delete %s %s: %s", resource_type, display_name, exc)

        append_entry(make_entry(
            resource_id=resource_id,
            resource_type=resource_type,
            display_name=display_name,
            size_bytes=size_bytes,
            action="delete",
            dry_run=self.dry_run,
            success=success,
            message=message,
            reason=decision.reason,
            tags=tags,
        ))

        return CleanupAction(
            resource_id=resource_id,
            resource_type=resource_type,
            action="delete",
            dry_run=self.dry_run,
            success=success,
            message=message,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_display(self, resource_type: str, resource_id: str) -> tuple[str, int, list[str]]:
        """Return (display_name, size_bytes, tags) for a resource without deleting it.

        Tags are only populated for images — they are the pull addresses needed
        for recovery after deletion.
        """
        try:
            if resource_type == "image":
                img = self.client.images.get(resource_id)
                name = img.tags[0] if img.tags else resource_id[:12]
                return name, img.attrs.get("Size", 0), list(img.tags)
            if resource_type == "container":
                ctr = self.client.containers.get(resource_id)
                return ctr.name, 0, []
            if resource_type == "volume":
                vol = self.client.volumes.get(resource_id)
                return vol.name, 0, []
            if resource_type == "network":
                net = self.client.networks.get(resource_id)
                return net.name, 0, []
        except Exception:  # noqa: BLE001
            pass
        return resource_id[:12], 0, []

    def _do_delete(self, resource_type: str, resource_id: str) -> None:
        """Call the appropriate Docker API removal method."""
        if resource_type == "image":
            self.client.images.remove(resource_id, force=False)
        elif resource_type == "container":
            ctr = self.client.containers.get(resource_id)
            ctr.remove(force=True)
        elif resource_type == "volume":
            vol = self.client.volumes.get(resource_id)
            vol.remove()
        elif resource_type == "network":
            net = self.client.networks.get(resource_id)
            net.remove()
        else:
            raise ValueError(f"Unknown resource type: {resource_type}")
