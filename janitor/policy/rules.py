"""Policy rule definitions loaded from configuration."""

from dataclasses import dataclass


@dataclass
class RetentionRule:
    """Keep resources created within the last N days."""
    max_age_days: int


@dataclass
class PatternRule:
    """Keep resources whose name/tag matches a pattern (supports wildcards)."""
    pattern: str


@dataclass
class MinVersionsRule:
    """Keep at least N versions of each image repository."""
    min_versions: int


@dataclass
class PolicyRules:
    retention: RetentionRule | None = None
    keep_patterns: list[PatternRule] | None = None
    min_versions: MinVersionsRule | None = None
    protect_running: bool = True
    protect_named: bool = True
