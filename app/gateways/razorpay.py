import time
import uuid
from typing import Optional

from app.core.enums import GatewayType
from app.gateways.base import (
    GatewayAuthRequest,
    GatewayAuthResponse,
    GatewayCaptureRequest,
    GatewayCaptureResponse,
    GatewayRefundRequest,
    GatewayRefundResponse,
    GatewayStatusQueryRequest,
    GatewayStatusQueryResponse,
    PaymentGateway,
)


class RazorpayGateway(PaymentGateway):
    """Mock adapter for Razorpay Payment Gateway."""

    def __init__(self, gateway_id: str = "razorpay"):
        super().__init__(gateway_id=gateway_id, gateway_type=GatewayType.RAZORPAY)

    async def authorize(self, request: GatewayAuthRequest) -> GatewayAuthResponse:
        start_time = time.monotonic()
        await self._handle_mock_response(request.mock_header)

        # Standard Razorpay Payment Intent mock
        rzp_payment_id = f"pay_{uuid.uuid4().hex[:14]}"
        rzp_rrn = f"RRN_{uuid.uuid4().hex[:12].upper()}"
        latency_ms = int((time.monotonic() - start_time) * 1000)

        raw_payload = {
            "id": rzp_payment_id,
            "entity": "payment",
            "amount": request.amount,
            "currency": request.currency,
            "status": "authorized",
            "order_id": f"order_{request.order_id}",
            "method": request.payment_method.lower(),
            "captured": False,
            "description": "Payment authorization via Razorpay",
            "acquirer_data": {
                "rrn": rzp_rrn,
                "auth_code": "654321",
            },
        }

        return GatewayAuthResponse(
            success=True,
            gateway_transaction_id=rzp_payment_id,
            gateway_reference_id=rzp_rrn,
            status="AUTHORIZED",
            raw_payload=raw_payload,
            latency_ms=latency_ms,
        )

    async def capture(self, request: GatewayCaptureRequest) -> GatewayCaptureResponse:
        start_time = time.monotonic()
        await self._handle_mock_response(request.mock_header)

        latency_ms = int((time.monotonic() - start_time) * 1000)
        raw_payload = {
            "id": request.gateway_transaction_id,
            "entity": "payment",
            "status": "captured",
            "amount": request.amount,
            "currency": request.currency,
        }

        return GatewayCaptureResponse(
            success=True,
            gateway_transaction_id=request.gateway_transaction_id,
            status="CAPTURED",
            captured_amount=request.amount,
            raw_payload=raw_payload,
            latency_ms=latency_ms,
        )

    async def refund(self, request: GatewayRefundRequest) -> GatewayRefundResponse:
        start_time = time.monotonic()
        await self._handle_mock_response(request.mock_header)

        latency_ms = int((time.monotonic() - start_time) * 1000)
        refund_id = f"rfnd_{uuid.uuid4().hex[:14]}"
        raw_payload = {
            "id": refund_id,
            "payment_id": request.gateway_transaction_id,
            "amount": request.amount,
            "currency": request.currency,
            "status": "processed",
        }

        return GatewayRefundResponse(
            success=True,
            gateway_refund_id=refund_id,
            refunded_amount=request.amount,
            status="REFUNDED",
            raw_payload=raw_payload,
            latency_ms=latency_ms,
        )

    async def query_status(self, request: GatewayStatusQueryRequest) -> GatewayStatusQueryResponse:
        start_time = time.monotonic()
        await self._handle_mock_response(request.mock_header)
        latency_ms = int((time.monotonic() - start_time) * 1000)

        gw_id = request.gateway_transaction_id or f"pay_{request.transaction_id.hex[:14]}"
        resolved_status = self._status_registry.get(gw_id, "AUTHORIZED")

        raw_payload = {
            "id": gw_id,
            "entity": "payment",
            "status": "captured" if resolved_status == "CAPTURED" else "authorized",
            "reconciliation_status": resolved_status,
        }

        return GatewayStatusQueryResponse(
            success=resolved_status in ("AUTHORIZED", "CAPTURED"),
            gateway_transaction_id=gw_id,
            status=resolved_status,
            raw_payload=raw_payload,
            latency_ms=latency_ms,
        )

