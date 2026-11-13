import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_db
from app.schemas.webhook import WebhookDLQEventResponse, WebhookIngestResultResponse
from app.services.webhook import (
    InvalidWebhookSignatureException,
    MissingWebhookSignatureException,
    WebhookProcessingException,
    webhook_ingestion_service,
)

logger = logging.getLogger("payment_layer.webhook_api")

router = APIRouter()


@router.post(
    "/{gateway}",
    response_model=WebhookIngestResultResponse,
    status_code=status.HTTP_200_OK,
    summary="Ingest PSP webhook notification",
    description="""
    Securely ingests incoming payment webhooks across Razorpay, Stripe, PayU, and UPI:
    1. HMAC-SHA256 signature verification with timingSafeEqual (constant-time).
    2. Exactly-once deduplication via composite primary key (gateway, event_id).
    3. Returns HTTP 200 immediately if duplicate or already acknowledged.
    4. Automatically routes to Dead Letter Queue (DLQ) if processing fails 3 times.
    """,
)
async def process_incoming_webhook(
    gateway: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Ingests and processes PSP webhook callbacks.
    Enforces HMAC-SHA256 signature verification, deduplication, and DLQ protection.
    """
    raw_body = await request.body()
    headers = dict(request.headers)

    try:
        result = await webhook_ingestion_service.ingest_webhook(
            gateway=gateway,
            raw_body=raw_body,
            headers=headers,
            db=db,
        )
        return result

    except MissingWebhookSignatureException as exc:
        logger.warning("Webhook rejected: missing signature for gateway '%s': %s", gateway, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing webhook signature header for gateway '{gateway}': {exc!s}",
        )

    except InvalidWebhookSignatureException as exc:
        logger.warning(
            "Webhook rejected: invalid HMAC signature for gateway '%s': %s",
            gateway,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid webhook signature for gateway '{gateway}': {exc!s}",
        )

    except WebhookProcessingException as exc:
        logger.error("Webhook processing failed for gateway '%s': %s", gateway, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get(
    "/dlq/events",
    response_model=list[WebhookDLQEventResponse],
    status_code=status.HTTP_200_OK,
    summary="Query Dead Letter Queue (DLQ) webhooks",
)
async def list_dlq_webhooks(
    gateway: str | None = Query(None, description="Filter by gateway identifier"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_async_db),
):
    """Retrieves all webhooks currently parked in the Dead Letter Queue."""
    events = await webhook_ingestion_service.get_dlq_events(
        db=db, gateway=gateway, limit=limit, offset=offset
    )
    return events


@router.post(
    "/dlq/{gateway}/{event_id}/replay",
    response_model=WebhookDLQEventResponse,
    status_code=status.HTTP_200_OK,
    summary="Replay a Dead Letter Queue (DLQ) webhook",
)
async def replay_dlq_webhook(
    gateway: str,
    event_id: str,
    db: AsyncSession = Depends(get_async_db),
):
    """Manually triggers replay of a dead-lettered webhook event."""
    try:
        replayed_event = await webhook_ingestion_service.replay_dlq_event(
            gateway=gateway, event_id=event_id, db=db
        )
        return replayed_event
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except WebhookProcessingException as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Replay failed: {exc!s}",
        )
