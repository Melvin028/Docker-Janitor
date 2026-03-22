"""Base notifier interface."""

from abc import ABC, abstractmethod
from typing import Any


class BaseNotifier(ABC):
    """All notifiers must implement this interface."""

    @abstractmethod
    def report(self, result: Any) -> None:
        """Send or display the cleanup/scan result."""
