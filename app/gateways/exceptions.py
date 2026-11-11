from typing import Optional


class GatewayException(Exception):
    """Base exception for all payment gateway adapter errors."""
    def __init__(self, message: str, gateway_id: Optional[str] = None, error_code: Optional[str] = None):
        self.gateway_id = gateway_id
        self.error_code = error_code or "GATEWAY_ERROR"
        super().__init__(message)


class GatewayTimeoutException(GatewayException):
    """Raised when an external gateway call times out or encounters network latency."""
    def __init__(self, message: str = "Gateway operation timed out", gateway_id: Optional[str] = None):
        super().__init__(message=message, gateway_id=gateway_id, error_code="GATEWAY_TIMEOUT")


class GatewayDeclinedException(GatewayException):
    """Raised when a gateway or issuing bank rejects/declines an authorization/capture."""
    def __init__(
        self,
        message: str = "Transaction declined by issuing bank",
        gateway_id: Optional[str] = None,
        decline_code: Optional[str] = None,
    ):
        self.decline_code = decline_code or "DO_NOT_HONOR"
        super().__init__(message=message, gateway_id=gateway_id, error_code="PAYMENT_DECLINED")


class GatewayServiceUnavailableException(GatewayException):
    """Raised when an external gateway returns 5xx, maintenance mode, or TCP connection drop."""
    def __init__(self, message: str = "Gateway service temporarily unavailable", gateway_id: Optional[str] = None):
        super().__init__(message=message, gateway_id=gateway_id, error_code="GATEWAY_UNAVAILABLE")


class CircuitBreakerOpenException(GatewayException):
    """
    Raised when an invocation is fast-failed because the gateway's Circuit Breaker is OPEN.
    Enables immediate failover to alternate gateways without incurring upstream network latency.
    """
    def __init__(
        self,
        gateway_id: str,
        consecutive_failures: int,
        retry_after_seconds: float,
        message: Optional[str] = None,
    ):
        self.gateway_id = gateway_id
        self.consecutive_failures = consecutive_failures
        self.retry_after_seconds = retry_after_seconds
        msg = message or (
            f"Circuit breaker for gateway '{gateway_id}' is OPEN "
            f"after {consecutive_failures} consecutive failures. "
            f"Failing fast. Retry/probing available in {retry_after_seconds:.2f}s."
        )
        super().__init__(message=msg, gateway_id=gateway_id, error_code="CIRCUIT_BREAKER_OPEN")
