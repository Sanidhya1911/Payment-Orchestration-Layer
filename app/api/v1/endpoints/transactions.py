import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_async_db
from app.core.enums import IdempotencyStatus, StateTrigger, TransactionState, TransactionStatus
from app.models.idempotency import IdempotencyKeys
from app.models.transaction import Transactions, TransactionStateLog
from app.schemas.transaction import (
    TransactionCreateRequest,
    TransactionResponse,
    TransactionTransitionRequest,
)
from app.services.idempotency import (
    IdempotencyService,
    get_idempotency_service,
)
from app.services.state_machine import (
    InvalidStateTransitionException,
    TransactionNotFoundException,
    TransactionStateMachine,
)

router = APIRouter()


@router.post("", response_model=TransactionResponse, status_code=status.HTTP_201_CREATED)
async def create_transaction(
    payload: TransactionCreateRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_async_db),
    idemp_service: IdempotencyService = Depends(get_idempotency_service),
):
    """
    Initiates a new payment transaction with strict BIGINT financial precision (paise/cents)
    and mission-critical idempotency & PostgreSQL advisory locking.
    """
    async with idemp_service.idempotent_scope(
        session=db,
        key=idempotency_key,
        merchant_id=payload.merchant_id,
        request_path="/api/v1/transactions",
        payload=payload.model_dump(),
    ) as idemp:
        if idemp.is_cached:
            return idemp.cached_response

        # 1. Financial Precision: Amount is guaranteed BIGINT (paise/cents)
        transaction = Transactions(
            merchant_id=payload.merchant_id,
            order_id=payload.order_id,
            customer_id=payload.customer_id,
            amount=payload.amount,  # integer paise/cents
            currency=payload.currency,
            status=TransactionState.CREATED.value,
            payment_method=payload.payment_method.value,
            payment_method_details=payload.payment_method_details,
            metadata_=payload.metadata,
        )
        db.add(transaction)
        await db.flush()

        # 2. Append-only Immutable State Log Audit Trail
        state_log = TransactionStateLog(
            transaction_id=transaction.id,
            from_state=None,
            to_state=TransactionState.CREATED.value,
            trigger=StateTrigger.API_REQUEST.value,
            actor_id=payload.merchant_id,
            gateway_payload=None,
            transition_metadata={"reason": "Transaction initiated via Client API"},
        )
        db.add(state_log)
        await db.flush()

        # Refresh to include state log relation
        stmt = (
            select(Transactions)
            .options(selectinload(Transactions.state_logs))
            .where(Transactions.id == transaction.id)
        )
        res = await db.execute(stmt)
        transaction_loaded = res.scalar_one()

        # 3. Cache response in Idempotency Service
        response_data = TransactionResponse.model_validate(transaction_loaded).model_dump(mode="json")
        idemp.set_response(status_code=status.HTTP_201_CREATED, body=response_data)

        return transaction_loaded


@router.post("/idempotency/cleanup", status_code=status.HTTP_200_OK, tags=["Idempotency Maintenance"])
async def cleanup_expired_idempotency_keys(
    db: AsyncSession = Depends(get_async_db),
    idemp_service: IdempotencyService = Depends(get_idempotency_service),
):
    """
    Expires and cleans up idempotency keys older than 24 hours.
    """
    count = await idemp_service.cleanup_expired_keys(session=db)
    return {
        "status": "success",
        "pruned_count": count,
        "message": f"Successfully purged {count} expired idempotency keys.",
    }


@router.get("/{transaction_id}", response_model=TransactionResponse)
async def get_transaction(
    transaction_id: uuid.UUID,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Fetches full transaction record including its complete immutable audit state log history.
    """
    stmt = (
        select(Transactions)
        .options(selectinload(Transactions.state_logs))
        .where(Transactions.id == transaction_id)
    )
    result = await db.execute(stmt)
    transaction = result.scalar_one_or_none()
    if not transaction:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction {transaction_id} not found."
        )
    return transaction


@router.post("/{transaction_id}/transitions", response_model=TransactionResponse, status_code=status.HTTP_200_OK)
async def transition_transaction(
    transaction_id: uuid.UUID,
    payload: TransactionTransitionRequest,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Executes a deterministic state machine transition on a transaction.
    Enforces pessimistic locking (SELECT FOR UPDATE) and automatically writes
    an immutable audit log into transaction_state_log.
    """
    sm = TransactionStateMachine()
    try:
        await sm.transition(
            transaction_id=transaction_id,
            target_state=payload.target_state,
            trigger=payload.trigger,
            actor_id=payload.actor_id,
            gateway_payload=payload.gateway_payload,
            transition_metadata=payload.transition_metadata,
            gateway_id=payload.gateway_id,
            gateway_transaction_id=payload.gateway_transaction_id,
            gateway_reference_id=payload.gateway_reference_id,
            error_code=payload.error_code,
            error_message=payload.error_message,
            session=db,
        )

        # Refresh transaction with eagerly loaded state logs
        stmt = (
            select(Transactions)
            .options(selectinload(Transactions.state_logs))
            .where(Transactions.id == transaction_id)
        )
        res = await db.execute(stmt)
        return res.scalar_one()

    except InvalidStateTransitionException as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc)
        )
    except TransactionNotFoundException as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc)
        )


