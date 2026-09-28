"""
Base adapter classes for external module integration.

This module provides abstract base classes for creating adapters that
bridge external modules with Waddles's module system.
"""

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from ..base import BaseModule, ExecuteRequest, ExecuteResponse


@dataclass
class HealthStatus:
    """
    Health status tracking for external modules.

    Attributes:
        is_healthy: Current health status
        last_success: Timestamp of last successful execution
        last_failure: Timestamp of last failed execution
        consecutive_failures: Number of consecutive failures
        total_requests: Total number of requests made
        total_failures: Total number of failed requests
        error_rate: Current error rate (0.0 to 1.0)
    """
    is_healthy: bool = True
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    consecutive_failures: int = 0
    total_requests: int = 0
    total_failures: int = 0
    error_rate: float = 0.0

    def record_success(self) -> None:
        """Record a successful execution."""
        self.is_healthy = True
        self.last_success = datetime.utcnow()
        self.consecutive_failures = 0
        self.total_requests += 1
        self._update_error_rate()

    def record_failure(self) -> None:
        """Record a failed execution."""
        self.last_failure = datetime.utcnow()
        self.consecutive_failures += 1
        self.total_requests += 1
        self.total_failures += 1
        self._update_error_rate()

        # Mark as unhealthy after 3 consecutive failures
        if self.consecutive_failures >= 3:
            self.is_healthy = False

    def _update_error_rate(self) -> None:
        """Update the error rate based on total requests and failures."""
        if self.total_requests > 0:
            self.error_rate = self.total_failures / self.total_requests
        else:
            self.error_rate = 0.0

    def get_status(self) -> Dict[str, Any]:
        """
        Get the current health status as a dictionary.

        Returns:
            Dictionary containing health status information
        """
        return {
            "is_healthy": self.is_healthy,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_failure": self.last_failure.isoformat() if self.last_failure else None,
            "consecutive_failures": self.consecutive_failures,
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "error_rate": round(self.error_rate, 4),
        }


class BaseAdapter(BaseModule):
    """
    Abstract base class for module adapters.

    Adapters bridge external modules (webhook endpoints, serverless functions, etc.)
    with Waddles's internal module system. They handle communication, error handling,
    and health tracking.

    Attributes:
        health: Health status tracker for this adapter
    """

    def __init__(self):
        """Initialize the adapter with health tracking."""
        super().__init__()
        self.health = HealthStatus()

    @abstractmethod
    async def execute_async(self, request: ExecuteRequest) -> ExecuteResponse:
        """
        Execute the module asynchronously.

        Args:
            request: ExecuteRequest containing command, args, and context

        Returns:
            ExecuteResponse with results of the execution

        Raises:
            NotImplementedError: Must be implemented by subclasses
        """
        raise NotImplementedError("Subclasses must implement execute_async()")

    def execute(self, request: ExecuteRequest) -> ExecuteResponse:
        """
        Synchronous execute method (delegates to async implementation).

        This method is provided for compatibility with BaseModule.
        In production, use execute_async() directly.

        Args:
            request: ExecuteRequest containing command, args, and context

        Returns:
            ExecuteResponse with results of the execution
        """
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.run_until_complete(self.execute_async(request))

    def get_health_status(self) -> Dict[str, Any]:
        """
        Get the current health status of the adapter.

        Returns:
            Dictionary containing health status information
        """
        return self.health.get_status()

    def is_healthy(self) -> bool:
        """
        Check if the adapter is currently healthy.

        Returns:
            True if healthy, False otherwise
        """
        return self.health.is_healthy
