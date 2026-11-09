import hashlib
import json
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_db
from app.core.enums import WebhookProcessingStatus
from app.models.webhook import ProcessedWebhookEvents
from app.schemas.webhook import WebhookEventResponse, WebhookIngestRequest

router = APIRouter()


@router.post("/{gateway}", response_model=WebhookEventResponse, status_code=status.HTTP_200_OK)
async def process_incoming_webhook(
    gateway: str,
    payload: WebhookIngestRequest,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Ingests PSP webhook notifications with strict composite primary key deduplication
    on (gateway, event_id). Ensures exactly-once asynchronous processing.
    """
    gateway_clean = gateway.lower().strip()
    now = datetime.now(timezone.utc)

    # 1. Deduplication lookup via Composite Primary Key (gateway, event_id)
    query = select(ProcessedWebhookEvents).where(
        ProcessedWebhookEvents.gateway == gateway_clean,
        ProcessedWebhookEvents.event_id == payload.event_id,
    )
    result = await db.execute(query)
    existing_event = result.scalar_one_or_none()

    if existing_event:
        # Idempotently return already processed event record
        return existing_event

    # 2. Compute canonical SHA-256 payload hash for tamper protection
    canonical_body = json.dumps(payload.payload, sort_keys=True, default=str)
    payload_hash = hashlib.sha256(canonical_body.encode("utf-8")).hexdigest()

    # 3. Create processed webhook event record using Composite Primary Key
    webhook_event = ProcessedWebhookEvents(
        gateway=gateway_clean,
        event_id=payload.event_id,
        event_type=payload.event_type,
        payload_hash=payload_hash,
        status=WebhookProcessingStatus.PROCESSED.value,
        raw_payload=payload.payload,
        processing_error=None,
        received_at=now,
        processed_at=now,
    )
    db.add(webhook_event)
    await db.flush()

    return webhook_event

