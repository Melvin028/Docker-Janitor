"""Policy Engine — applies user-defined rules and marks resources safe or unsafe to delete."""

import fnmatch
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from janitor.scanner.models import ContainerInfo, ImageInfo, ScanResult, VolumeInfo
from janitor.policy.rules import MinVersionsRule, PatternRule, PolicyRules, RetentionRule
from janitor.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ResourceDecision:
    resource_id: str
    resource_type: str
    safe_to_delete: bool
    reason: str


@dataclass
class PolicyResult:
    decisions: list[ResourceDecision] = field(default_factory=list)
    scan_result: ScanResult | None = None


class PolicyEngine:
    """Evaluates scan results against configured rules and produces deletion decisions."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.rules = self._load_rules(config.get("policy", {}))

    def evaluate(self, scan_result: ScanResult) -> PolicyResult:
        """Apply rules to a ScanResult and return a PolicyResult."""
        logger.info("Evaluating policy rules...")
        decisions: list[ResourceDecision] = []
        decisions.extend(self._evaluate_images(scan_result.images))
        decisions.extend(self._evaluate_containers(scan_result.containers))
        decisions.extend(self._evaluate_volumes(scan_result.volumes))
        return PolicyResult(decisions=decisions, scan_result=scan_result)

    # ------------------------------------------------------------------
    # Rule loading
    # ------------------------------------------------------------------

    def _load_rules(self, policy_config: dict[str, Any]) -> PolicyRules:
        """Deserialize the ``policy:`` config block into a typed :class:`PolicyRules`."""
        retention = None
        if (days := policy_config.get("retention_days")) is not None:
            retention = RetentionRule(max_age_days=int(days))

        keep_patterns = None
        if raw_patterns := policy_config.get("keep_patterns"):
            keep_patterns = [PatternRule(pattern=str(p)) for p in raw_patterns]

        min_versions = None
        if (mv := policy_config.get("min_versions")) is not None and int(mv) > 0:
            min_versions = MinVersionsRule(min_versions=int(mv))

        return PolicyRules(
            retention=retention,
            keep_patterns=keep_patterns,
            min_versions=min_versions,
            protect_running=bool(policy_config.get("protect_running", True)),
            protect_named=bool(policy_config.get("protect_named", True)),
        )

    # ------------------------------------------------------------------
    # Image evaluation
    # ------------------------------------------------------------------

    def _evaluate_images(self, images: list[ImageInfo]) -> list[ResourceDecision]:
        """Apply the guard chain to every image and return one decision per image."""
        protected_by_min_versions: set[str] = set()
        if self.rules.min_versions:
            protected_by_min_versions = self._min_version_protected_ids(
                images, self.rules.min_versions.min_versions
            )

        decisions: list[ResourceDecision] = []
        for img in images:
            decision = self._decide_image(img, protected_by_min_versions)
            logger.debug(
                "%s → safe_to_delete=%s (%s)",
                img.display_name,
                decision.safe_to_delete,
                decision.reason,
            )
            decisions.append(decision)

        return decisions

    def _decide_image(
        self, img: ImageInfo, protected_by_min_versions: set[str]
    ) -> ResourceDecision:
        """Run a single image through each guard in priority order.

        Guards are evaluated from broadest safety net to most specific.
        The first guard that fires returns a KEEP decision immediately.
        Images that pass every guard are marked safe to delete.
        """
        # Guard 1 — never delete images used by a running container
        if self.rules.protect_running and img.in_use:
            return ResourceDecision(
                resource_id=img.id,
                resource_type="image",
                safe_to_delete=False,
                reason="in use by a running container",
            )

        # Guard 2 — never delete images that carry at least one named tag
        if self.rules.protect_named:
            named_tags = [t for t in img.tags if not t.startswith("sha256:")]
            if named_tags:
                return ResourceDecision(
                    resource_id=img.id,
                    resource_type="image",
                    safe_to_delete=False,
                    reason=f"has named tag(s): {', '.join(named_tags)}",
                )

        # Guard 3 — keep_patterns: any tag matches a wildcard glob
        if self.rules.keep_patterns:
            for tag in img.tags:
                matched = self._matches_any_pattern(tag, self.rules.keep_patterns)
                if matched:
                    return ResourceDecision(
                        resource_id=img.id,
                        resource_type="image",
                        safe_to_delete=False,
                        reason=f"tag '{tag}' matches keep pattern '{matched}'",
                    )

        # Guard 4 — min_versions: image is among the N newest for its repo
        if img.id in protected_by_min_versions:
            return ResourceDecision(
                resource_id=img.id,
                resource_type="image",
                safe_to_delete=False,
                reason="within min_versions threshold for its repository",
            )

        # Guard 5 — retention: image is younger than max_age_days
        if self.rules.retention and img.age_days < self.rules.retention.max_age_days:
            return ResourceDecision(
                resource_id=img.id,
                resource_type="image",
                safe_to_delete=False,
                reason=(
                    f"only {img.age_days}d old "
                    f"(retention threshold: {self.rules.retention.max_age_days}d)"
                ),
            )

        # Passed every guard — safe to remove
        return ResourceDecision(
            resource_id=img.id,
            resource_type="image",
            safe_to_delete=True,
            reason=f"unused, {img.age_days}d old, no matching keep rules",
        )

    # ------------------------------------------------------------------
    # Container evaluation
    # ------------------------------------------------------------------

    def _evaluate_containers(self, containers: list[ContainerInfo]) -> list[ResourceDecision]:
        """Flag stopped containers older than container_retention_days.

        Opt-in: does nothing when ``container_retention_days`` is 0 (default).
        Running containers are never flagged.
        """
        policy_cfg = self.config.get("policy", {})
        retention_days = int(policy_cfg.get("container_retention_days", 0))
        if retention_days == 0:
            return []

        decisions: list[ResourceDecision] = []
        for c in containers:
            if c.is_running:
                decisions.append(ResourceDecision(
                    resource_id=c.id,
                    resource_type="container",
                    safe_to_delete=False,
                    reason="container is running",
                ))
            elif c.age_days < retention_days:
                decisions.append(ResourceDecision(
                    resource_id=c.id,
                    resource_type="container",
                    safe_to_delete=False,
                    reason=f"only {c.age_days}d old (container retention: {retention_days}d)",
                ))
            else:
                decisions.append(ResourceDecision(
                    resource_id=c.id,
                    resource_type="container",
                    safe_to_delete=True,
                    reason=f"stopped container, {c.age_days}d old (≥ {retention_days}d threshold)",
                ))
        return decisions

    # ------------------------------------------------------------------
    # Volume evaluation
    # ------------------------------------------------------------------

    def _evaluate_volumes(self, volumes: list[VolumeInfo]) -> list[ResourceDecision]:
        """Flag orphaned volumes (not mounted by any container).

        Opt-in: does nothing when ``cleanup_orphaned_volumes`` is false (default).
        """
        policy_cfg = self.config.get("policy", {})
        if not policy_cfg.get("cleanup_orphaned_volumes", False):
            return []

        decisions: list[ResourceDecision] = []
        for v in volumes:
            if v.in_use:
                decisions.append(ResourceDecision(
                    resource_id=v.name,
                    resource_type="volume",
                    safe_to_delete=False,
                    reason="volume is mounted by a container",
                ))
            else:
                decisions.append(ResourceDecision(
                    resource_id=v.name,
                    resource_type="volume",
                    safe_to_delete=True,
                    reason="orphaned volume (not mounted by any container)",
                ))
        return decisions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _matches_any_pattern(tag: str, patterns: list[PatternRule]) -> str | None:
        """Return the first matching pattern string, or ``None`` if none match.

        Both the full ``repo:label`` string and the bare label are checked so
        that a pattern like ``"latest"`` matches ``"nginx:latest"``.
        """
        label = tag.split(":")[-1]
        for rule in patterns:
            if fnmatch.fnmatch(tag, rule.pattern) or fnmatch.fnmatch(label, rule.pattern):
                return rule.pattern
        return None

    @staticmethod
    def _min_version_protected_ids(images: list[ImageInfo], min_versions: int) -> set[str]:
        """Return IDs of the *N* newest images per repository.

        Images are grouped by the repo portion of their first tag
        (e.g. ``"nginx"`` from ``"nginx:1.25"``). Untagged (dangling) images
        are grouped under the sentinel key ``"<dangling>"`` and are never
        protected by this rule.
        """
        repo_groups: dict[str, list[ImageInfo]] = defaultdict(list)

        for img in images:
            if not img.tags:
                continue  # dangling images are not protected by min_versions
            repo = img.tags[0].split(":")[0]
            repo_groups[repo].append(img)

        protected: set[str] = set()
        for group in repo_groups.values():
            newest = sorted(group, key=lambda i: i.created_at, reverse=True)
            for img in newest[:min_versions]:
                protected.add(img.id)

        return protected
