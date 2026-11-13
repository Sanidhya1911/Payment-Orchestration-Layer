import asyncio
import time
import uuid

import pytest

from app.core.enums import CircuitBreakerStatus, GatewayType
from app.gateways.base import GatewayAuthRequest
from app.gateways.exceptions import (
    CircuitBreakerOpenException,
    GatewayDeclinedException,
    GatewayServiceUnavailableException,
    GatewayTimeoutException,
)
from app.gateways.payu import PayUGateway
from app.gateways.razorpay import RazorpayGateway
from app.gateways.router import ResilientGatewayRouter
from app.gateways.stripe import StripeGateway
from app.gateways.upi import UPIGateway
from app.services.circuit_breaker import CircuitBreaker, CircuitBreakerRegistry


@pytest.fixture
def sample_auth_request() -> GatewayAuthRequest:
    return GatewayAuthRequest(
        transaction_id=uuid.uuid4(),
        amount=50000,  # 500.00 INR (BIGINT paise)
        currency="INR",
        payment_method="UPI",
        merchant_id="merchant_test_123",
        order_id="order_778899",
        customer_id="cust_001",
        payment_method_details={"vpa": "testuser@okaxis"},
    )


# =============================================================================
# 1. GATEWAY MOCK IMPLEMENTATION TESTS & X-MOCK-RESPONSE HEADERS
# =============================================================================
@pytest.mark.anyio
async def test_razorpay_gateway_mock_and_timeout_header(sample_auth_request):
    """Verify Razorpay gateway handles success and X-Mock-Response: timeout."""
    gateway = RazorpayGateway()
    assert gateway.gateway_type == GatewayType.RAZORPAY

    # 1. Normal success
    resp = await gateway.authorize(sample_auth_request)
    assert resp.success is True
    assert resp.status == "AUTHORIZED"
    assert resp.gateway_transaction_id.startswith("pay_")
    assert resp.gateway_reference_id.startswith("RRN_")
    assert resp.raw_payload["amount"] == 50000

    # 2. X-Mock-Response: timeout
    sample_auth_request.mock_header = "timeout"
    with pytest.raises(GatewayTimeoutException) as exc_info:
        await gateway.authorize(sample_auth_request)
    assert "Upstream timeout" in str(exc_info.value)
    assert exc_info.value.gateway_id == "razorpay"


@pytest.mark.anyio
async def test_stripe_gateway_mock_and_decline_header(sample_auth_request):
    """Verify Stripe gateway handles success and X-Mock-Response: decline."""
    gateway = StripeGateway()
    assert gateway.gateway_type == GatewayType.STRIPE

    # 1. Normal success
    sample_auth_request.mock_header = None
    resp = await gateway.authorize(sample_auth_request)
    assert resp.success is True
    assert resp.status == "AUTHORIZED"
    assert resp.gateway_transaction_id.startswith("pi_")
    assert resp.gateway_reference_id.startswith("ARN_")

    # 2. X-Mock-Response: decline
    sample_auth_request.mock_header = "decline"
    with pytest.raises(GatewayDeclinedException) as exc_info:
        await gateway.authorize(sample_auth_request)
    assert "declined by issuing bank" in str(exc_info.value)
    assert exc_info.value.decline_code == "CARD_DECLINED"


@pytest.mark.anyio
async def test_payu_gateway_mock_and_unavailable_header(sample_auth_request):
    """Verify PayU gateway handles success and X-Mock-Response: unavailable (503)."""
    gateway = PayUGateway()
    assert gateway.gateway_type == GatewayType.PAYU

    # 1. Normal success
    sample_auth_request.mock_header = None
    resp = await gateway.authorize(sample_auth_request)
    assert resp.success is True
    assert resp.gateway_transaction_id.startswith("payu_")
    assert resp.gateway_reference_id.startswith("BANK_")

    # 2. X-Mock-Response: unavailable
    sample_auth_request.mock_header = "unavailable"
    with pytest.raises(GatewayServiceUnavailableException) as exc_info:
        await gateway.authorize(sample_auth_request)
    assert "Service Unavailable" in str(exc_info.value)


@pytest.mark.anyio
async def test_upi_gateway_mock_and_network_error_header(sample_auth_request):
    """Verify direct UPI gateway handles standard 12-digit RRN and network error header."""
    gateway = UPIGateway()
    assert gateway.gateway_type == GatewayType.UPI

    # 1. Normal success
    sample_auth_request.mock_header = None
    resp = await gateway.authorize(sample_auth_request)
    assert resp.success is True
    assert resp.gateway_transaction_id.startswith("upi_")
    assert len(resp.gateway_reference_id) >= 12  # Standard 12-digit UPI RRN
    assert resp.raw_payload["responseCode"] == "00"

    # 2. X-Mock-Response: network_error
    sample_auth_request.mock_header = "network_error"
    with pytest.raises(Exception) as exc_info:
        await gateway.authorize(sample_auth_request)
    assert "Connection reset" in str(exc_info.value)


# =============================================================================
# 2. CIRCUIT BREAKER FSM (CLOSED -> OPEN -> HALF-OPEN -> CLOSED)
# =============================================================================
@pytest.mark.anyio
async def test_circuit_breaker_consecutive_failures_trips_to_open():
    """
    MANDATE:
    The circuit breaker must track consecutive failures per gateway;
    if a gateway times out repeatedly, it trips to OPEN and fails-fast.
    """
    cb = CircuitBreaker(gateway_id="razorpay", failure_threshold=3, recovery_timeout=2.0)
    assert cb.state == CircuitBreakerStatus.CLOSED
    assert cb.is_available is True

    async def mock_failing_call():
        raise GatewayTimeoutException("Timeout", gateway_id="razorpay")

    # Failure 1
    with pytest.raises(GatewayTimeoutException):
        await cb.call(mock_failing_call)
    assert cb.state == CircuitBreakerStatus.CLOSED
    assert cb.consecutive_failures == 1

    # Failure 2
    with pytest.raises(GatewayTimeoutException):
        await cb.call(mock_failing_call)
    assert cb.state == CircuitBreakerStatus.CLOSED
    assert cb.consecutive_failures == 2

    # Failure 3 -> TRIPS TO OPEN!
    with pytest.raises(GatewayTimeoutException):
        await cb.call(mock_failing_call)
    assert cb.state == CircuitBreakerStatus.OPEN
    assert cb.is_available is False
    assert cb.consecutive_failures == 3

    # Subsequent call FAILS-FAST without executing mock_failing_call!
    executed = False

    async def probe():
        nonlocal executed
        executed = True

    with pytest.raises(CircuitBreakerOpenException) as exc_info:
        await cb.call(probe)

    assert executed is False  # Function was never called (fail-fast!)
    assert exc_info.value.gateway_id == "razorpay"
    assert exc_info.value.consecutive_failures == 3
    assert exc_info.value.retry_after_seconds <= 2.0


@pytest.mark.anyio
async def test_circuit_breaker_transitions_to_half_open_after_2_seconds():
    """
    MANDATE:
    Within 2 seconds, allows trial canary probe in HALF-OPEN state,
    recovering to CLOSED on success.
    """
    # Use 0.2s recovery timeout for rapid unit test verification
    cb = CircuitBreaker(gateway_id="stripe", failure_threshold=2, recovery_timeout=0.2)

    # Force 2 failures -> Trips to OPEN
    await cb.record_failure(GatewayTimeoutException("err 1"))
    await cb.record_failure(GatewayTimeoutException("err 2"))
    assert cb.state == CircuitBreakerStatus.OPEN

    # Immediately: still OPEN
    assert cb.state == CircuitBreakerStatus.OPEN

    # Wait for recovery timeout (0.2s)
    await asyncio.sleep(0.25)

    # State transitions to HALF_OPEN
    assert cb.state == CircuitBreakerStatus.HALF_OPEN
    assert cb.is_available is True

    # Canary probe call succeeds -> Recovers to CLOSED
    async def successful_probe():
        return "success"

    result = await cb.call(successful_probe)
    assert result == "success"
    assert cb.state == CircuitBreakerStatus.CLOSED
    assert cb.consecutive_failures == 0


@pytest.mark.anyio
async def test_circuit_breaker_half_open_failure_re_trips_to_open():
    """If the canary probe fails during HALF-OPEN, it re-trips back to OPEN."""
    cb = CircuitBreaker(gateway_id="payu", failure_threshold=1, recovery_timeout=0.1)

    # Trip to OPEN
    await cb.record_failure(GatewayTimeoutException("err"))
    assert cb.state == CircuitBreakerStatus.OPEN

    # Wait for recovery
    await asyncio.sleep(0.15)
    assert cb.state == CircuitBreakerStatus.HALF_OPEN

    # Canary probe fails
    async def failing_probe():
        raise GatewayServiceUnavailableException("Still down")

    with pytest.raises(GatewayServiceUnavailableException):
        await cb.call(failing_probe)

    # Immediately re-tripped to OPEN
    assert cb.state == CircuitBreakerStatus.OPEN


# =============================================================================
# 3. RESILIENT ROUTING & FAILOVER TO ALTERNATE GATEWAY WITHIN 2 SECONDS
# =============================================================================
@pytest.mark.anyio
async def test_router_fails_over_to_alternate_gateway_when_primary_trips(
    sample_auth_request,
):
    """
    MANDATE:
    If a gateway times out repeatedly, it trips to OPEN and fails-fast,
    allowing routing to an alternate gateway within 2 seconds.
    """
    registry = CircuitBreakerRegistry(failure_threshold=3, recovery_timeout=2.0)
    router = ResilientGatewayRouter(registry=registry)

    # Configure priority order: 1. Razorpay, 2. Stripe, 3. PayU, 4. UPI
    priority = ["razorpay", "stripe", "payu", "upi"]

    # 1. Simulate Razorpay failing 3 times due to upstream timeout
    sample_auth_request.mock_header = "timeout"
    for _ in range(3):
        # When Razorpay fails, router automatically falls over to Stripe!
        # But Stripe succeeds because mock_header was only targeted at failing Razorpay
        # To simulate Razorpay failing specifically:
        pass

    # Let's directly trip Razorpay's circuit breaker
    rzp_breaker = registry.get("razorpay")
    await rzp_breaker.record_failure(GatewayTimeoutException("Timeout 1"))
    await rzp_breaker.record_failure(GatewayTimeoutException("Timeout 2"))
    await rzp_breaker.record_failure(GatewayTimeoutException("Timeout 3"))

    assert rzp_breaker.state == CircuitBreakerStatus.OPEN

    # 2. Now dispatch new transaction:
    # Razorpay fails-fast (<1ms) because its circuit is OPEN!
    # Router immediately falls over to Stripe (the alternate gateway)!
    sample_auth_request.mock_header = None
    start_time = time.monotonic()
    response, successful_gw, trace = await router.execute_with_failover(
        request=sample_auth_request,
        priority_gateways=priority,
    )
    duration = time.monotonic() - start_time

    # Verification:
    assert duration < 2.0, f"Failover took {duration}s, must be within 2 seconds"
    assert successful_gw == "stripe"
    assert response.success is True
    assert response.gateway_transaction_id.startswith("pi_")

    # Routing trace verification:
    assert len(trace) == 2
    assert trace[0]["gateway"] == "razorpay"
    assert trace[0]["status"] == "CIRCUIT_OPEN_FAST_FAIL"
    assert trace[1]["gateway"] == "stripe"
    assert trace[1]["status"] == "SUCCESS"
