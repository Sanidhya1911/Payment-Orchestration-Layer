import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import (
    Any,
    TypeVar,
)

from app.core.enums import CircuitBreakerStatus
from app.gateways.exceptions import (
    CircuitBreakerOpenException,
    GatewayServiceUnavailableException,
    GatewayTimeoutException,
)

logger = logging.getLogger("payment_layer.circuit_breaker")

T = TypeVar("T")


class CircuitBreaker:
    """
    Finite-State Circuit Breaker enforcing high-availability gateway resilience.

    States:
    - CLOSED: Normal operation. Successes reset failure counts. Consecutive failures trip to OPEN.
    - OPEN: Fails-fast immediately without invoking the external gateway. Allows 2-second failover.
    - HALF_OPEN: After 2 seconds recovery timeout, permits a trial canary probe to test recovery.
    """

    def __init__(
        self,
        gateway_id: str,
        failure_threshold: int = 3,
        recovery_timeout: float = 2.0,
        half_open_success_threshold: int = 1,
    ):
        self.gateway_id = gateway_id
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_success_threshold = half_open_success_threshold

        # Internal state
        self._state: CircuitBreakerStatus = CircuitBreakerStatus.CLOSED
        self._consecutive_failures: int = 0
        self._consecutive_successes: int = 0
        self._tripped_at: float | None = None
        self._last_state_change: float = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitBreakerStatus:
        """Current circuit breaker state, checking for recovery timeout transition if OPEN."""
        if self._state == CircuitBreakerStatus.OPEN:
            elapsed = time.monotonic() - (self._tripped_at or 0.0)
            if elapsed >= self.recovery_timeout:
                return CircuitBreakerStatus.HALF_OPEN
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def is_available(self) -> bool:
        """Returns True if the circuit breaker will allow an execution attempt."""
        return self.state in (
            CircuitBreakerStatus.CLOSED,
            CircuitBreakerStatus.HALF_OPEN,
        )

    async def record_success(self) -> None:
        """Records a successful gateway call, handling recovery transitions."""
        async with self._lock:
            current_state = self.state

            if current_state == CircuitBreakerStatus.HALF_OPEN:
                self._consecutive_successes += 1
                if self._consecutive_successes >= self.half_open_success_threshold:
                    # Successfully recovered!
                    self._state = CircuitBreakerStatus.CLOSED
                    self._consecutive_failures = 0
                    self._consecutive_successes = 0
                    self._tripped_at = None
                    self._last_state_change = time.monotonic()
                    logger.info(
                        "Circuit breaker for gateway '%s' successfully RECOVERED to CLOSED",
                        self.gateway_id,
                    )
            elif current_state == CircuitBreakerStatus.CLOSED:
                self._consecutive_failures = 0

    async def record_failure(self, error: Exception) -> None:
        """Records a gateway failure (timeout, 5xx), tripping to OPEN if threshold exceeded."""
        async with self._lock:
            current_state = self.state

            if current_state == CircuitBreakerStatus.HALF_OPEN:
                # Canary probe failed -> immediately trip back to OPEN for another recovery period
                self._state = CircuitBreakerStatus.OPEN
                self._tripped_at = time.monotonic()
                self._consecutive_successes = 0
                self._last_state_change = time.monotonic()
                logger.warning(
                    "Canary probe failed for gateway '%s'. Tripping back to OPEN for %.1fs. Error: %s",
                    self.gateway_id,
                    self.recovery_timeout,
                    str(error),
                )
            elif current_state == CircuitBreakerStatus.CLOSED:
                self._consecutive_failures += 1
                logger.warning(
                    "Gateway '%s' recorded failure (%d/%d): %s",
                    self.gateway_id,
                    self._consecutive_failures,
                    self.failure_threshold,
                    str(error),
                )
                if self._consecutive_failures >= self.failure_threshold:
                    # Trip to OPEN
                    self._state = CircuitBreakerStatus.OPEN
                    self._tripped_at = time.monotonic()
                    self._consecutive_successes = 0
                    self._last_state_change = time.monotonic()
                    logger.error(
                        "Circuit breaker for gateway '%s' TRIPPED to OPEN after %d consecutive failures. "
                        "Failing-fast for next %.1fs.",
                        self.gateway_id,
                        self._consecutive_failures,
                        self.recovery_timeout,
                    )

    async def call(self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """
        Executes a gateway coroutine wrapped with circuit breaker protection:
        - If OPEN: raises CircuitBreakerOpenException immediately without calling func (<1ms).
        - If CLOSED or HALF_OPEN: invokes func, tracking success or failure.
        """
        # 1. State check
        current = self.state

        if current == CircuitBreakerStatus.OPEN:
            elapsed = time.monotonic() - (self._tripped_at or 0.0)
            remaining = max(0.0, self.recovery_timeout - elapsed)
            raise CircuitBreakerOpenException(
                gateway_id=self.gateway_id,
                consecutive_failures=self._consecutive_failures,
                retry_after_seconds=remaining,
            )

        # 2. Invoke gateway operation
        try:
            result = await func(*args, **kwargs)
            await self.record_success()
            return result
        except (GatewayTimeoutException, GatewayServiceUnavailableException) as exc:
            # Critical infrastructural failures trigger circuit breaker tracking
            await self.record_failure(exc)
            raise exc
        except Exception as exc:
            # Non-circuit breaker errors (e.g. client validation) do not trip the circuit
            raise exc

    def reset(self) -> None:
        """Manually resets the circuit breaker to CLOSED."""
        self._state = CircuitBreakerStatus.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._tripped_at = None
        self._last_state_change = time.monotonic()
        logger.info("Circuit breaker for gateway '%s' manually RESET to CLOSED", self.gateway_id)


class CircuitBreakerRegistry:
    """Registry managing Circuit Breaker singletons per gateway adapter."""

    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 2.0):
        self.default_failure_threshold = failure_threshold
        self.default_recovery_timeout = recovery_timeout
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = asyncio.Lock()

    def get(self, gateway_id: str) -> CircuitBreaker:
        """Retrieves or initializes a circuit breaker for a gateway."""
        gid = gateway_id.lower().strip()
        if gid not in self._breakers:
            self._breakers[gid] = CircuitBreaker(
                gateway_id=gid,
                failure_threshold=self.default_failure_threshold,
                recovery_timeout=self.default_recovery_timeout,
            )
        return self._breakers[gid]

    def reset_all(self) -> None:
        """Resets all registered circuit breakers."""
        for cb in self._breakers.values():
            cb.reset()


# Global Singleton Registry
circuit_breaker_registry = CircuitBreakerRegistry()
