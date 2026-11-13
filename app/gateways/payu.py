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
    GatewayStatusQueryRequest,
    GatewayStatusQueryResponse,
    PaymentGateway,
)


class PayUGateway(PaymentGateway):
    """Mock adapter for PayU India Payment Gateway."""

    def __init__(self, gateway_id: str = "payu"):
        super().__init__(gateway_id=gateway_id, gateway_type=GatewayType.PAYU)

    async def authorize(self, request: GatewayAuthRequest) -> GatewayAuthResponse:
        start_time = time.monotonic()
        await self._handle_mock_response(request.mock_header)

        mihpayid = str(uuid.uuid4().int)[:10]
        payu_txnid = f"payu_{uuid.uuid4().hex[:14]}"
        bank_ref_num = f"BANK_{uuid.uuid4().hex[:8].upper()}"
        latency_ms = int((time.monotonic() - start_time) * 1000)

        raw_payload = {
            "mihpayid": mihpayid,
            "mode": request.payment_method.upper(),
            "status": "success",
            "unmappedstatus": "auth",
            "txnid": payu_txnid,
            "amount": request.amount,
            "bank_ref_num": bank_ref_num,
            "bankcode": "HDFC",
            "error_Message": "No Error",
        }

        return GatewayAuthResponse(
            success=True,
            gateway_transaction_id=payu_txnid,
            gateway_reference_id=bank_ref_num,
            status="AUTHORIZED",
            raw_payload=raw_payload,
            latency_ms=latency_ms,
        )

    async def capture(self, request: GatewayCaptureRequest) -> GatewayCaptureResponse:
        start_time = time.monotonic()
        await self._handle_mock_response(request.mock_header)

        latency_ms = int((time.monotonic() - start_time) * 1000)
        raw_payload = {
            "status": 1,
            "msg": "Transaction Captured Successfully",
            "txnid": request.gateway_transaction_id,
            "amount": request.amount,
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
        refund_id = f"payu_rfnd_{uuid.uuid4().hex[:10]}"
        raw_payload = {
            "status": 1,
            "msg": "Refund Request Accepted",
            "request_id": refund_id,
            "amount": request.amount,
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

        gw_id = request.gateway_transaction_id or f"payu_{request.transaction_id.hex[:10]}"
        resolved_status = self._status_registry.get(gw_id, "AUTHORIZED")

        raw_payload = {
            "mihpayid": gw_id,
            "status": ("success" if resolved_status in ("AUTHORIZED", "CAPTURED") else "failure"),
            "reconciliation_status": resolved_status,
        }

        return GatewayStatusQueryResponse(
            success=resolved_status in ("AUTHORIZED", "CAPTURED"),
            gateway_transaction_id=gw_id,
            status=resolved_status,
            raw_payload=raw_payload,
            latency_ms=latency_ms,
        )
