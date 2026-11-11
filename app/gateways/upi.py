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


class UPIGateway(PaymentGateway):
    """Mock adapter for direct Unified Payments Interface (UPI) rail."""

    def __init__(self, gateway_id: str = "upi"):
        super().__init__(gateway_id=gateway_id, gateway_type=GatewayType.UPI)

    async def authorize(self, request: GatewayAuthRequest) -> GatewayAuthResponse:
        start_time = time.monotonic()
        await self._handle_mock_response(request.mock_header)

        upi_txn_id = f"upi_{uuid.uuid4().hex[:14]}"
        rrn = f"{time.strftime('%y%j%H')}{str(uuid.uuid4().int)[:6]}"  # 12-digit standard UPI RRN
        latency_ms = int((time.monotonic() - start_time) * 1000)

        vpa = (request.payment_method_details or {}).get("vpa", "customer@upi")

        raw_payload = {
            "responseCode": "00",
            "responseMessage": "UPI TRANSACTION SUCCESS",
            "upiTranlogId": upi_txn_id,
            "rrn": rrn,
            "payerVpa": vpa,
            "amount": request.amount,
            "status": "SUCCESS",
            "txnTime": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        return GatewayAuthResponse(
            success=True,
            gateway_transaction_id=upi_txn_id,
            gateway_reference_id=rrn,
            status="AUTHORIZED",
            raw_payload=raw_payload,
            latency_ms=latency_ms,
        )

    async def capture(self, request: GatewayCaptureRequest) -> GatewayCaptureResponse:
        start_time = time.monotonic()
        await self._handle_mock_response(request.mock_header)

        # In direct UPI rail, authorize and capture are typically single-message settlements
        latency_ms = int((time.monotonic() - start_time) * 1000)
        raw_payload = {
            "responseCode": "00",
            "status": "SETTLED",
            "upiTranlogId": request.gateway_transaction_id,
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
        refund_rrn = f"RFND_{uuid.uuid4().hex[:10].upper()}"
        raw_payload = {
            "responseCode": "00",
            "status": "REFUND_SUCCESS",
            "originalTxnId": request.gateway_transaction_id,
            "refundRrn": refund_rrn,
            "amount": request.amount,
        }

        return GatewayRefundResponse(
            success=True,
            gateway_refund_id=refund_rrn,
            refunded_amount=request.amount,
            status="REFUNDED",
            raw_payload=raw_payload,
            latency_ms=latency_ms,
        )
