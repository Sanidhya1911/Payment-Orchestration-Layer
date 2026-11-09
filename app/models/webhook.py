from datetime import datetime
from typing import Optional
from sqlalchemy import (
    DateTime,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
from app.core.enums import WebhookProcessingStatus


class ProcessedWebhookEvents(Base):
    """
    Deduplication store for incoming PSP webhook callbacks.
    Guarantees idempotency on asynchronous gateway notifications.
    Strictly uses a composite primary key of (gateway, event_id).
    """
    __tablename__ = "processed_webhook_events"

    # COMPOSITE PRIMARY KEY (gateway, event_id)
    gateway: Mapped[str] = mapped_column(
        String(50),
        primary_key=True,
        comment="Gateway identifier (e.g., 'stripe', 'razorpay', 'adyen')"
    )
    event_id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        comment="PSP-provided unique event/notification identifier"
    )

    event_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Normalized or raw event type (e.g. payment_intent.succeeded)"
    )
    payload_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA-256 hash of the incoming raw webhook request body"
    )
    status: Mapped[str] = mapped_column(
        String(30),
        default=WebhookProcessingStatus.RECEIVED.value,
        nullable=False,
        index=True
    )
    raw_payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="Raw JSON webhook payload for audit and asynchronous replays"
    )
    processing_error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Stack trace or business rejection reason if status=FAILED"
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )

