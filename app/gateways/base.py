import abc
import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.core.enums import GatewayType
from app.gateways.exceptions import (
    GatewayDeclinedException,
    GatewayException,
    GatewayServiceUnavailableException,
    GatewayTimeoutException,
)


@dataclass
class GatewayAuthRequest:
    """Standardized authorization request payload passed to gateway adapters."""
    transaction_id: uuid.UUID
    amount: int  # Strict BIGINT in minor currency units (paise/cents)
    currency: str  # ISO 4217 (e.g., INR, USD)
    payment_method: str  # CARD, UPI, NETBANKING
    merchant_id: str
    order_id: str
    customer_id: Optional[str] = None
    payment_method_details: Optional[Dict[str, Any]] = None
    mock_header: Optional[str] = None  # Passed from X-Mock-Response header


@dataclass
class GatewayAuthResponse:
    """Standardized response from gateway authorization attempt."""
    success: bool
    gateway_transaction_id: str
    gateway_reference_id: Optional[str]
    status: str
    raw_payload: Dict[str, Any]
    latency_ms: int
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class GatewayCaptureRequest:
    transaction_id: uuid.UUID
    gateway_transaction_id: str
    amount: int  # Strict BIGINT
    currency: str
    mock_header: Optional[str] = None


@dataclass
class GatewayCaptureResponse:
    success: bool
    gateway_transaction_id: str
    status: str
    captured_amount: int
    raw_payload: Dict[str, Any]
    latency_ms: int
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class GatewayRefundRequest:
    transaction_id: uuid.UUID
    gateway_transaction_id: str
    amount: int  # Strict BIGINT
    currency: str
    reason: Optional[str] = None
    mock_header: Optional[str] = None


@dataclass
class GatewayRefundResponse:
    success: bool
    gateway_refund_id: str
    refunded_amount: int
    status: str
    raw_payload: Dict[str, Any]
    latency_ms: int
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class PaymentGateway(abc.ABC):
    """
    Abstract Base Adapter defining the standard PSP interaction contract.
    All gateway adapters (Razorpay, Stripe, PayU, UPI) conform to this interface.
    """

    def __init__(self, gateway_id: str, gateway_type: GatewayType):
        self.gateway_id = gateway_id
        self.gateway_type = gateway_type

    @abc.abstractmethod
    async def authorize(self, request: GatewayAuthRequest) -> GatewayAuthResponse:
        """Executes payment authorization against the PSP."""
        pass

    @abc.abstractmethod
    async def capture(self, request: GatewayCaptureRequest) -> GatewayCaptureResponse:
        """Settles an authorized payment."""
        pass

    @abc.abstractmethod
    async def refund(self, request: GatewayRefundRequest) -> GatewayRefundResponse:
        """Issues full or partial refund for settled funds."""
        pass

    async def _handle_mock_response(self, mock_header: Optional[str]) -> None:
        """
        Interprets X-Mock-Response simulation headers to test resilience,
        timeouts, circuit breaker tripping, and bank declines.
        """
        if not mock_header:
            return

        mock_cmd = mock_header.strip().lower()

        if mock_cmd == "timeout":
            # Simulate upstream network hang / timeout
            await asyncio.sleep(0.05)
            raise GatewayTimeoutException(
                message=f"Upstream timeout from gateway '{self.gateway_id}'",
                gateway_id=self.gateway_id,
            )

        if mock_cmd in ("decline", "declined"):
            raise GatewayDeclinedException(
                message=f"Transaction declined by issuing bank via gateway '{self.gateway_id}'",
                gateway_id=self.gateway_id,
                decline_code="CARD_DECLINED",
            )

        if mock_cmd in ("unavailable", "500", "503"):
            raise GatewayServiceUnavailableException(
                message=f"Gateway '{self.gateway_id}' returned HTTP 503 Service Unavailable",
                gateway_id=self.gateway_id,
            )

        if mock_cmd == "network_error":
            raise GatewayException(
                message=f"Connection reset by peer on '{self.gateway_id}'",
                gateway_id=self.gateway_id,
                error_code="CONNECTION_RESET",
            )

