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
from app.core.enums import IdempotencyStatus, StateTrigger, TransactionStatus
from app.models.idempotency import IdempotencyKeys
from app.models.transaction import Transactions, TransactionStateLog
from app.schemas.transaction import (
    TransactionCreateRequest,
    TransactionResponse,
)

router = APIRouter()


@router.post("", response_model=TransactionResponse, status_code=status.HTTP_201_CREATED)
async def create_transaction(
    payload: TransactionCreateRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Initiates a new payment transaction with strict BIGINT financial precision (paise/cents)
    and mission-critical idempotency safety.
    """
    now = datetime.now(timezone.utc)
    idempotency_record = None

    # 1. Idempotency Check & Distributed Locking
    if idempotency_key:
        # Compute SHA-256 payload hash
        canonical_payload = json.dumps(payload.model_dump(), sort_keys=True, default=str)
        payload_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()

        # Check existing key for merchant
        query = select(IdempotencyKeys).where(
            IdempotencyKeys.key == idempotency_key,
            IdempotencyKeys.merchant_id == payload.merchant_id
        )
        result = await db.execute(query)
        idempotency_record = result.scalar_one_or_none()

        if idempotency_record:
            # If completed, return cached response
            if idempotency_record.status == IdempotencyStatus.COMPLETED.value:
                if idempotency_record.request_hash != payload_hash:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="Idempotency key reused with mismatched request payload.",
                    )
                return idempotency_record.response_body

            # If pending and currently locked, reject concurrent collision
            if idempotency_record.locked_until and idempotency_record.locked_until > now:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A transaction with this idempotency key is currently in-flight. Please retry shortly.",
                    headers={"Retry-After": "2"}
                )
        else:
            # Acquire lock for new idempotency key
            idempotency_record = IdempotencyKeys(
                key=idempotency_key,
                merchant_id=payload.merchant_id,
                request_path="/api/v1/transactions",
                request_hash=payload_hash,
                status=IdempotencyStatus.PENDING.value,
                locked_until=now + timedelta(seconds=30),
                expires_at=now + timedelta(hours=24),
            )
            db.add(idempotency_record)
            await db.flush()

    # 2. Financial Precision: Amount is guaranteed BIGINT (paise/cents)
    transaction = Transactions(
        merchant_id=payload.merchant_id,
        order_id=payload.order_id,
        customer_id=payload.customer_id,
        amount=payload.amount,  # integer paise/cents
        currency=payload.currency,
        status=TransactionStatus.INITIATED.value,
        payment_method=payload.payment_method.value,
        payment_method_details=payload.payment_method_details,
        metadata_=payload.metadata,
    )
    db.add(transaction)
    await db.flush()

    # 3. Append-only Immutable State Log Audit Trail
    state_log = TransactionStateLog(
        transaction_id=transaction.id,
        from_state=None,
        to_state=TransactionStatus.INITIATED.value,
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

    # 4. Resolve Idempotency Key Record
    if idempotency_record:
        response_data = TransactionResponse.model_validate(transaction_loaded).model_dump(mode="json")
        idempotency_record.status = IdempotencyStatus.COMPLETED.value
        idempotency_record.response_code = status.HTTP_201_CREATED
        idempotency_record.response_body = response_data
        idempotency_record.locked_until = None

    return transaction_loaded


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
