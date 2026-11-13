import logging
import uuid
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_async_db
from app.core.enums import StateTrigger, TransactionState
from app.core.errors import PaymentAPIException, format_uniform_error_dict
from app.gateways.base import (
    GatewayAuthRequest,
    GatewayCaptureRequest,
    GatewayRefundRequest,
)
from app.gateways.exceptions import (
    CircuitBreakerOpenException,
    GatewayDeclinedException,
    GatewayException,
    GatewayTimeoutException,
)
from app.gateways.router import GatewayRouter, gateway_router
from app.models.transaction import Transactions, TransactionStateLog
from app.schemas.payment import (
    PaymentCaptureRequest,
    PaymentCreateRequest,
    PaymentRefundRequest,
    PaymentResponse,
)
from app.schemas.webhook import WebhookIngestResultResponse
from app.services.idempotency import (
    IdempotencyService,
    get_idempotency_service,
)
from app.services.reconciliation import (
    ReconciliationEngine,
    reconciliation_engine,
)
from app.services.state_machine import (
    InvalidStateTransitionException,
    TransactionNotFoundException,
    TransactionStateMachine,
)
from app.services.webhook import webhook_ingestion_service

logger = logging.getLogger("payment_layer.payments_api")

router = APIRouter()


# =============================================================================
# 1. POST /api/v1/payments (Initiate & Process Payment)
# =============================================================================

@router.post("", response_model=PaymentResponse, status_code=status.HTTP_201_CREATED)
async def create_payment(
    payload: PaymentCreateRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    x_mock_response: Optional[str] = Header(None, alias="X-Mock-Response"),
    db: AsyncSession = Depends(get_async_db),
    idemp_service: IdempotencyService = Depends(get_idempotency_service),
):
    """
    Initiates, routes, and authorizes a new payment transaction with:
    - Strict BIGINT precision (paise/cents).
    - Distributed idempotency locking via PostgreSQL advisory locks.
    - Multi-criteria dynamic routing and automated failover.
    - Deterministic transaction state machine logging.
    """
    # 1. Idempotency Scope Context
    async with idemp_service.idempotent_scope(
        session=db,
        key=idempotency_key,
        merchant_id=payload.merchant_id,
        request_path="/api/v1/payments",
        payload=payload.model_dump(),
    ) as idemp:
        if idemp.is_cached:
            return idemp.cached_response

        # 2. Financial Amount: Strict BIGINT minor units
        txn = Transactions(
            id=uuid.uuid4(),
            merchant_id=payload.merchant_id,
            order_id=payload.order_id,
            customer_id=payload.customer_id,
            amount=payload.amount,
            currency=payload.currency,
            status=TransactionState.CREATED.value,
            payment_method=payload.payment_method.value,
            payment_method_details=payload.payment_method_details,
            metadata_=payload.metadata,
        )
        db.add(txn)
        await db.flush()

        # Audit initial creation
        log_create = TransactionStateLog(
            transaction_id=txn.id,
            from_state=None,
            to_state=TransactionState.CREATED.value,
            trigger=StateTrigger.API_REQUEST.value,
            actor_id=payload.merchant_id,
            transition_metadata={"reason": "Payment initiated via API"},
        )
        db.add(log_create)
        await db.flush()

        sm = TransactionStateMachine()

        # 3. Select Route via Multi-Criteria Gateway Router
        auth_req = GatewayAuthRequest(
            transaction_id=txn.id,
            amount=payload.amount,
            currency=payload.currency,
            payment_method=payload.payment_method.value,
            merchant_id=payload.merchant_id,
            order_id=payload.order_id,
            customer_id=payload.customer_id,
            payment_method_details=payload.payment_method_details,
            mock_header=x_mock_response,
        )

        priority_order = [payload.preferred_gateway] if payload.preferred_gateway else None

        # Transition to ROUTE_SELECTED
        await sm.transition(
            transaction_id=txn.id,
            target_state=TransactionState.ROUTE_SELECTED,
            trigger=StateTrigger.ROUTE_SELECTED,
            actor_id=payload.merchant_id,
            session=db,
        )

        # 4. Execute with Resilient Router (Pessimistic lock released before gateway call!)
        try:
            # Transition to AUTH_INITIATED
            await sm.transition(
                transaction_id=txn.id,
                target_state=TransactionState.AUTH_INITIATED,
                trigger=StateTrigger.GATEWAY_CALL,
                actor_id=payload.merchant_id,
                session=db,
            )

            # Gateway call executed without holding DB lock
            gw_resp, chosen_gw, _ = await gateway_router.execute_with_failover(
                request=auth_req,
                priority_gateways=priority_order,
            )

            # 5. On Gateway Success: Transition to AUTHORISED
            await sm.transition(
                transaction_id=txn.id,
                target_state=TransactionState.AUTHORISED,
                trigger=StateTrigger.GATEWAY_RESPONSE,
                actor_id=chosen_gw,
                gateway_id=chosen_gw,
                gateway_transaction_id=gw_resp.gateway_transaction_id,
                gateway_reference_id=gw_resp.gateway_reference_id,
                gateway_payload=gw_resp.raw_payload,
                session=db,
            )

            # Auto-capture for instant payment methods (e.g., UPI) adhering to state graph
            if payload.auto_capture and payload.payment_method.value == "UPI":
                await sm.transition(
                    transaction_id=txn.id,
                    target_state=TransactionState.CAPTURE_INITIATED,
                    trigger=StateTrigger.API_REQUEST,
                    actor_id=chosen_gw,
                    session=db,
                )
                await sm.transition(
                    transaction_id=txn.id,
                    target_state=TransactionState.CAPTURED,
                    trigger=StateTrigger.GATEWAY_RESPONSE,
                    actor_id=chosen_gw,
                    session=db,
                )
                txn.captured_amount = payload.amount

            await db.commit()
            await db.refresh(txn)

            # Cache response in idempotency service
            resp_data = PaymentResponse.model_validate(txn).model_dump(mode="json")
            idemp.set_response(status_code=status.HTTP_201_CREATED, body=resp_data)
            if idempotency_key:
                await idemp_service.save_response(
                    session=db,
                    key=idempotency_key,
                    merchant_id=payload.merchant_id,
                    response_code=status.HTTP_201_CREATED,
                    response_body=resp_data,
                )
                await db.commit()
            return txn

        except GatewayDeclinedException as exc:
            gw_actor = exc.gateway_id or payload.preferred_gateway or "gateway"
            await sm.transition(
                transaction_id=txn.id,
                target_state=TransactionState.AUTH_FAILED,
                trigger=StateTrigger.GATEWAY_RESPONSE,
                actor_id=gw_actor,
                error_code=exc.decline_code or "CARD_DECLINED",
                error_message=exc.message,
                session=db,
            )
            await db.commit()
            raise PaymentAPIException(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="PAYMENT_DECLINED",
                message=exc.message,
                gateway_error_code=exc.decline_code or "CARD_DECLINED",
            )

        except GatewayTimeoutException as exc:
            gw_actor = exc.gateway_id or payload.preferred_gateway or "gateway"
            await sm.transition(
                transaction_id=txn.id,
                target_state=TransactionState.AUTH_FAILED,
                trigger=StateTrigger.GATEWAY_TIMEOUT,
                actor_id=gw_actor,
                error_code="TIMEOUT",
                error_message=exc.message,
                session=db,
            )
            await db.commit()
            raise PaymentAPIException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                code="GATEWAY_TIMEOUT",
                message=exc.message,
                gateway_error_code="TIMEOUT",
            )

        except CircuitBreakerOpenException as exc:
            gw_actor = exc.gateway_id or "circuit_breaker"
            await sm.transition(
                transaction_id=txn.id,
                target_state=TransactionState.FAILED,
                trigger=StateTrigger.GATEWAY_RESPONSE,
                actor_id=gw_actor,
                error_code="CIRCUIT_OPEN",
                error_message=exc.message,
                session=db,
            )
            await db.commit()
            raise PaymentAPIException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="CIRCUIT_BREAKER_OPEN",
                message=exc.message,
                gateway_error_code="CIRCUIT_OPEN_FAST_FAIL",
            )

        except GatewayException as exc:
            gw_actor = exc.gateway_id or payload.preferred_gateway or "gateway"
            await sm.transition(
                transaction_id=txn.id,
                target_state=TransactionState.FAILED,
                trigger=StateTrigger.GATEWAY_RESPONSE,
                actor_id=gw_actor,
                error_code=exc.error_code or "GATEWAY_ERROR",
                error_message=exc.message,
                session=db,
            )
            await db.commit()
            raise PaymentAPIException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                code="GATEWAY_ERROR",
                message=exc.message,
                gateway_error_code=exc.error_code or "GATEWAY_ERROR",
            )


# =============================================================================
# 2. POST /api/v1/payments/{id}/capture (Capture Authorized Payment)
# =============================================================================

@router.post("/{id}/capture", response_model=PaymentResponse, status_code=status.HTTP_200_OK)
async def capture_payment(
    id: uuid.UUID,
    payload: Optional[PaymentCaptureRequest] = None,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    x_mock_response: Optional[str] = Header(None, alias="X-Mock-Response"),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Settles an authorized payment transaction.
    Enforces state machine progression: AUTHORISED -> CAPTURE_INITIATED -> CAPTURED.
    """
    stmt = select(Transactions).where(Transactions.id == id)
    res = await db.execute(stmt)
    txn = res.scalar_one_or_none()

    if not txn:
        raise PaymentAPIException(
            status_code=status.HTTP_404_NOT_FOUND,
            code="TRANSACTION_NOT_FOUND",
            message=f"Payment transaction '{id}' not found.",
            gateway_error_code=None,
        )

    # Validate state transition feasibility
    if txn.status not in (TransactionState.AUTHORISED.value, TransactionState.PARTIALLY_CAPTURED.value):
        raise PaymentAPIException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="INVALID_STATE_TRANSITION",
            message=f"Cannot capture transaction '{id}' in state '{txn.status}'. Must be AUTHORISED.",
            gateway_error_code=None,
        )

    capture_amount = payload.amount if (payload and payload.amount) else txn.amount
    if capture_amount <= 0:
        raise PaymentAPIException(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="INVALID_CAPTURE_AMOUNT",
            message="Capture amount must be greater than zero.",
            gateway_error_code=None,
        )

    sm = TransactionStateMachine()
    gateway_id = (txn.gateway_id or "stripe").lower().strip()
    gw = gateway_router.get_gateway(gateway_id)

    if not gw:
        raise PaymentAPIException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="GATEWAY_NOT_CONFIGURED",
            message=f"Gateway '{gateway_id}' is not configured for capture.",
            gateway_error_code=None,
        )

    # 1. Transition to CAPTURE_INITIATED under pessimistic lock
    await sm.transition(
        transaction_id=txn.id,
        target_state=TransactionState.CAPTURE_INITIATED,
        trigger=StateTrigger.API_REQUEST,
        actor_id=txn.merchant_id,
        session=db,
    )
    await db.commit()

    # 2. Invoke gateway capture (Lock is released before external API call)
    capture_req = GatewayCaptureRequest(
        transaction_id=txn.id,
        gateway_transaction_id=txn.gateway_transaction_id or str(txn.id),
        amount=capture_amount,
        currency=txn.currency,
        mock_header=x_mock_response,
    )

    try:
        cap_resp = await gw.capture(capture_req)
        is_partial = capture_amount < txn.amount
        target_state = TransactionState.PARTIALLY_CAPTURED if is_partial else TransactionState.CAPTURED

        await sm.transition(
            transaction_id=txn.id,
            target_state=target_state,
            trigger=StateTrigger.GATEWAY_RESPONSE,
            actor_id=gateway_id,
            gateway_payload=cap_resp.raw_payload,
            session=db,
        )

        await db.commit()
        await db.refresh(txn)
        return txn

    except GatewayDeclinedException as exc:
        await sm.transition(
            transaction_id=txn.id,
            target_state=TransactionState.CAPTURE_FAILED,
            trigger=StateTrigger.GATEWAY_RESPONSE,
            actor_id=gateway_id,
            error_code=exc.decline_code,
            error_message=exc.message,
            session=db,
        )
        await db.commit()
        raise PaymentAPIException(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="CAPTURE_DECLINED",
            message=exc.message,
            gateway_error_code=exc.decline_code or "CAPTURE_DECLINED",
        )

    except GatewayException as exc:
        await sm.transition(
            transaction_id=txn.id,
            target_state=TransactionState.CAPTURE_FAILED,
            trigger=StateTrigger.GATEWAY_RESPONSE,
            actor_id=gateway_id,
            error_code=exc.error_code,
            error_message=exc.message,
            session=db,
        )
        await db.commit()
        raise PaymentAPIException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="CAPTURE_FAILED",
            message=exc.message,
            gateway_error_code=exc.error_code or "CAPTURE_FAILED",
        )


# =============================================================================
# 3. POST /api/v1/payments/{id}/refund (Refund Settled Funds)
# =============================================================================

@router.post("/{id}/refund", response_model=PaymentResponse, status_code=status.HTTP_200_OK)
async def refund_payment(
    id: uuid.UUID,
    payload: Optional[PaymentRefundRequest] = None,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    x_mock_response: Optional[str] = Header(None, alias="X-Mock-Response"),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Issues full or partial refund for a captured or authorized payment.
    Enforces state machine progression: CAPTURED -> REFUND_INITIATED -> REFUNDED.
    """
    stmt = select(Transactions).where(Transactions.id == id)
    res = await db.execute(stmt)
    txn = res.scalar_one_or_none()

    if not txn:
        raise PaymentAPIException(
            status_code=status.HTTP_404_NOT_FOUND,
            code="TRANSACTION_NOT_FOUND",
            message=f"Payment transaction '{id}' not found.",
            gateway_error_code=None,
        )

    # Validate state transition feasibility
    refundable_states = (
        TransactionState.CAPTURED.value,
        TransactionState.PARTIALLY_CAPTURED.value,
        TransactionState.AUTHORISED.value,
    )
    if txn.status not in refundable_states:
        raise PaymentAPIException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="INVALID_STATE_TRANSITION",
            message=f"Cannot refund transaction '{id}' in state '{txn.status}'.",
            gateway_error_code=None,
        )

    refundable_balance = txn.amount - txn.refunded_amount
    refund_amount = payload.amount if (payload and payload.amount) else refundable_balance

    if refund_amount <= 0 or refund_amount > refundable_balance:
        raise PaymentAPIException(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="INVALID_REFUND_AMOUNT",
            message=f"Invalid refund amount {refund_amount}. Available refundable balance is {refundable_balance}.",
            gateway_error_code=None,
        )

    sm = TransactionStateMachine()
    gateway_id = (txn.gateway_id or "stripe").lower().strip()
    gw = gateway_router.get_gateway(gateway_id)

    if not gw:
        raise PaymentAPIException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="GATEWAY_NOT_CONFIGURED",
            message=f"Gateway '{gateway_id}' is not configured for refund.",
            gateway_error_code=None,
        )

    # 1. Transition to REFUND_INITIATED under pessimistic lock
    await sm.transition(
        transaction_id=txn.id,
        target_state=TransactionState.REFUND_INITIATED,
        trigger=StateTrigger.API_REQUEST,
        actor_id=txn.merchant_id,
        session=db,
    )
    await db.commit()

    # 2. Invoke gateway refund (Lock is released before external API call)
    refund_req = GatewayRefundRequest(
        transaction_id=txn.id,
        gateway_transaction_id=txn.gateway_transaction_id or str(txn.id),
        amount=refund_amount,
        currency=txn.currency,
        reason=payload.reason if payload else None,
        mock_header=x_mock_response,
    )

    try:
        ref_resp = await gw.refund(refund_req)

        await sm.transition(
            transaction_id=txn.id,
            target_state=TransactionState.REFUNDED,
            trigger=StateTrigger.GATEWAY_RESPONSE,
            actor_id=gateway_id,
            gateway_payload=ref_resp.raw_payload,
            session=db,
        )

        txn.refunded_amount += refund_amount
        await db.commit()
        await db.refresh(txn)
        return txn

    except GatewayException as exc:
        raise PaymentAPIException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="REFUND_FAILED",
            message=exc.message,
            gateway_error_code=exc.error_code or "REFUND_ERROR",
        )


# =============================================================================
# 4. WEBHOOKS & RECONCILIATION ROUTES
# =============================================================================

@router.post("/webhooks/{gateway}", response_model=WebhookIngestResultResponse, status_code=status.HTTP_200_OK)
async def process_payment_webhook(
    gateway: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    """Delegated route for webhooks under payments namespace."""
    raw_body = await request.body()
    headers = dict(request.headers)
    return await webhook_ingestion_service.ingest_webhook(
        gateway=gateway,
        raw_body=raw_body,
        headers=headers,
        db=db,
    )


@router.post("/reconciliation/run", status_code=status.HTTP_200_OK, tags=["Reconciliation"])
async def trigger_reconciliation_sweep(
    cutoff_minutes: int = 5,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Manually triggers a reconciliation cycle across stuck transactions.
    Identifies transactions in AUTH_INITIATED or CAPTURE_INITIATED for > cutoff_minutes,
    polls upstream gateways, and applies RECONCILIATION_OVERRIDE on mismatches.
    """
    report = await reconciliation_engine.reconcile_stuck_transactions(
        db=db, cutoff_minutes=cutoff_minutes
    )
    return report.to_dict()
