"""
Circuit Breaker Pattern Implementation

Provides fault tolerance for external service calls:
- Automatic failure detection
- Circuit opening after threshold failures
- Half-open state for recovery testing
- Exponential backoff retry logic
- Metrics tracking
"""

import logging
import asyncio
import time
from typing import Optional, Callable, Any
from enum import Enum
from functools import wraps
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject all requests
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Circuit breaker configuration"""
    failure_threshold: int = 5  # Failures before opening
    success_threshold: int = 2  # Successes in half-open before closing
    timeout_seconds: int = 60  # Time in open state before half-open
    expected_exception: type = Exception  # Exception type that triggers circuit


class CircuitBreakerError(Exception):
    """Raised when circuit breaker is open"""
    pass


class CircuitBreaker:
    """
    Circuit breaker for protecting against cascading failures.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, reject requests immediately
    - HALF_OPEN: Testing recovery, allow limited requests

    Usage:
        breaker = CircuitBreaker(
            name="api_service",
            failure_threshold=5,
            timeout_seconds=60
        )

        @breaker.call
        async def call_api():
            # Make API call
            pass
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        timeout_seconds: int = 60,
        expected_exception: type = Exception
    ):
        """
        Initialize circuit breaker.

        Args:
            name: Circuit breaker identifier
            failure_threshold: Failures before opening circuit
            success_threshold: Successes in half-open before closing
            timeout_seconds: Time in open state before attempting recovery
            expected_exception: Exception type that triggers circuit
        """
        self.name = name
        self.config = CircuitBreakerConfig(
            failure_threshold=failure_threshold,
            success_threshold=success_threshold,
            timeout_seconds=timeout_seconds,
            expected_exception=expected_exception
        )

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._last_state_change: float = time.time()

        # Metrics
        self._total_calls = 0
        self._total_failures = 0
        self._total_successes = 0
        self._total_rejections = 0

    @property
    def state(self) -> CircuitState:
        """Get current state"""
        return self._state

    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed (normal operation)"""
        return self._state == CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        """Check if circuit is open (rejecting requests)"""
        return self._state == CircuitState.OPEN

    @property
    def is_half_open(self) -> bool:
        """Check if circuit is half-open (testing recovery)"""
        return self._state == CircuitState.HALF_OPEN

    def _transition_to(self, new_state: CircuitState):
        """Transition to new state"""
        old_state = self._state
        self._state = new_state
        self._last_state_change = time.time()

        logger.info(
            f"Circuit breaker '{self.name}' transitioned: "
            f"{old_state.value} -> {new_state.value}"
        )

        # Reset counters on state change
        if new_state == CircuitState.CLOSED:
            self._failure_count = 0
            self._success_count = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._success_count = 0

    def _record_success(self):
        """Record successful call"""
        self._total_calls += 1
        self._total_successes += 1
        self._failure_count = 0  # Reset failure count on success

        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.config.success_threshold:
                self._transition_to(CircuitState.CLOSED)

    def _record_failure(self):
        """Record failed call"""
        self._total_calls += 1
        self._total_failures += 1
        self._failure_count += 1
        self._last_failure_time = time.time()

        if self._state == CircuitState.CLOSED:
            if self._failure_count >= self.config.failure_threshold:
                self._transition_to(CircuitState.OPEN)
        elif self._state == CircuitState.HALF_OPEN:
            # Any failure in half-open returns to open
            self._transition_to(CircuitState.OPEN)

    def _should_attempt_reset(self) -> bool:
        """Check if should attempt to transition from open to half-open"""
        if self._state != CircuitState.OPEN:
            return False

        if self._last_failure_time is None:
            return False

        elapsed = time.time() - self._last_failure_time
        return elapsed >= self.config.timeout_seconds

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection.

        Args:
            func: Async function to execute
            *args: Function arguments
            **kwargs: Function keyword arguments

        Returns:
            Function result

        Raises:
            CircuitBreakerError: If circuit is open
            Exception: If function raises exception
        """
        # Check if should attempt reset
        if self._should_attempt_reset():
            self._transition_to(CircuitState.HALF_OPEN)

        # Reject if open
        if self._state == CircuitState.OPEN:
            self._total_rejections += 1
            raise CircuitBreakerError(
                f"Circuit breaker '{self.name}' is OPEN. "
                f"Service unavailable."
            )

        # Attempt call
        try:
            result = await func(*args, **kwargs)
            self._record_success()
            return result

        except self.config.expected_exception as e:
            self._record_failure()
            logger.warning(
                f"Circuit breaker '{self.name}' recorded failure: {e}"
            )
            raise

    def __call__(self, func: Callable) -> Callable:
        """
        Decorator for protecting async functions.

        Usage:
            breaker = CircuitBreaker("service")

            @breaker
            async def call_service():
                # Make service call
                pass
        """
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await self.call(func, *args, **kwargs)
        return wrapper

    def get_metrics(self) -> dict:
        """Get circuit breaker metrics"""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "total_calls": self._total_calls,
            "total_failures": self._total_failures,
            "total_successes": self._total_successes,
            "total_rejections": self._total_rejections,
            "failure_rate": (
                self._total_failures / self._total_calls
                if self._total_calls > 0 else 0
            ),
            "last_state_change": self._last_state_change,
            "config": {
                "failure_threshold": self.config.failure_threshold,
                "success_threshold": self.config.success_threshold,
                "timeout_seconds": self.config.timeout_seconds
            }
        }

    def reset(self):
        """Manually reset circuit breaker to closed state"""
        logger.info(f"Circuit breaker '{self.name}' manually reset")
        self._transition_to(CircuitState.CLOSED)
        self._failure_count = 0
        self._success_count = 0


async def retry_with_backoff(
    func: Callable,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    exceptions: tuple = (Exception,)
) -> Any:
    """
    Retry async function with exponential backoff.

    Args:
        func: Async function to retry
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds
        max_delay: Maximum delay in seconds
        exponential_base: Base for exponential backoff
        exceptions: Tuple of exceptions to catch and retry

    Returns:
        Function result

    Raises:
        Last exception if all retries exhausted

    Example:
        result = await retry_with_backoff(
            lambda: api_call(),
            max_retries=3,
            initial_delay=1.0
        )
    """
    delay = initial_delay
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            if callable(func):
                result = await func()
            else:
                result = await func

            return result

        except exceptions as e:
            last_exception = e

            if attempt == max_retries:
                logger.error(
                    f"All {max_retries} retry attempts exhausted. "
                    f"Last error: {e}"
                )
                raise

            # Calculate delay with exponential backoff
            current_delay = min(
                delay * (exponential_base ** attempt),
                max_delay
            )

            logger.warning(
                f"Retry attempt {attempt + 1}/{max_retries} failed: {e}. "
                f"Retrying in {current_delay:.1f}s..."
            )

            await asyncio.sleep(current_delay)

    # Should never reach here, but just in case
    if last_exception:
        raise last_exception


def with_retry(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    exceptions: tuple = (Exception,)
):
    """
    Decorator for retry with exponential backoff.

    Usage:
        @with_retry(max_retries=3, initial_delay=1.0)
        async def call_api():
            # Make API call
            pass
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await retry_with_backoff(
                lambda: func(*args, **kwargs),
                max_retries=max_retries,
                initial_delay=initial_delay,
                max_delay=max_delay,
                exponential_base=exponential_base,
                exceptions=exceptions
            )
        return wrapper
    return decorator


class CircuitBreakerManager:
    """
    Manages multiple circuit breakers.

    Provides centralized management and monitoring of circuit breakers.
    """

    def __init__(self):
        """Initialize circuit breaker manager"""
        self._breakers: dict[str, CircuitBreaker] = {}

    def get_breaker(
        self,
        name: str,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        timeout_seconds: int = 60,
        expected_exception: type = Exception
    ) -> CircuitBreaker:
        """
        Get or create circuit breaker.

        Args:
            name: Circuit breaker name
            failure_threshold: Failures before opening
            success_threshold: Successes before closing
            timeout_seconds: Open state timeout
            expected_exception: Exception type to catch

        Returns:
            CircuitBreaker instance
        """
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                success_threshold=success_threshold,
                timeout_seconds=timeout_seconds,
                expected_exception=expected_exception
            )

        return self._breakers[name]

    def get_all_metrics(self) -> list[dict]:
        """Get metrics for all circuit breakers"""
        return [
            breaker.get_metrics()
            for breaker in self._breakers.values()
        ]

    def reset_all(self):
        """Reset all circuit breakers"""
        for breaker in self._breakers.values():
            breaker.reset()

    def get_breaker_by_name(self, name: str) -> Optional[CircuitBreaker]:
        """Get circuit breaker by name"""
        return self._breakers.get(name)
