import time
import uuid

from app.core.enums import GatewayType
from app.gateways.base import (
    GatewayAuthRequest,
    GatewayAuthResponse,
    GatewayCaptureRequest,
    GatewayCaptureResponse,
    GatewayRefundRequest,
    GatewayRefundResponse,
    PaymentGateway,
)


class StripeGateway(PaymentGateway):
    """Mock adapter for Stripe Payments."""

    def __init__(self, gateway_id: str = "stripe"):
        super().__init__(gateway_id=gateway_id, gateway_type=GatewayType.STRIPE)

    async def authorize(self, request: GatewayAuthRequest) -> GatewayAuthResponse:
        start_time = time.monotonic()
        await self._handle_mock_response(request.mock_header)

        pi_id = f"pi_{uuid.uuid4().hex[:24]}"
        ch_id = f"ch_{uuid.uuid4().hex[:24]}"
        arn = f"ARN_{uuid.uuid4().hex[:12].upper()}"
        latency_ms = int((time.monotonic() - start_time) * 1000)

        raw_payload = {
            "id": pi_id,
            "object": "payment_intent",
            "amount": request.amount,
            "currency": request.currency.lower(),
            "status": "requires_capture",
            "charges": {
                "data": [
                    {
                        "id": ch_id,
                        "paid": True,
                        "captured": False,
                        "payment_method_details": {
                            "type": request.payment_method.lower(),
                        },
                        "receipt_url": f"https://pay.stripe.com/receipts/{ch_id}",
                    }
                ]
            },
        }

        return GatewayAuthResponse(
            success=True,
            gateway_transaction_id=pi_id,
            gateway_reference_id=arn,
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
            "object": "payment_intent",
            "status": "succeeded",
            "amount_received": request.amount,
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
        re_id = f"re_{uuid.uuid4().hex[:24]}"
        raw_payload = {
            "id": re_id,
            "object": "refund",
            "amount": request.amount,
            "currency": request.currency.lower(),
            "payment_intent": request.gateway_transaction_id,
            "status": "succeeded",
        }

        return GatewayRefundResponse(
            success=True,
            gateway_refund_id=re_id,
            refunded_amount=request.amount,
            status="REFUNDED",
            raw_payload=raw_payload,
            latency_ms=latency_ms,
        )
