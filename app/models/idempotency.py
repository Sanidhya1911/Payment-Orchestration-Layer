from datetime import datetime
from typing import Optional
from sqlalchemy import (
    DateTime,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base
from app.core.enums import IdempotencyStatus
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class IdempotencyKeys(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Guarantees exactly-once transaction processing semantics.
    Tracks in-flight execution locks and caches successful responses.
    """
    __tablename__ = "idempotency_keys"

    __table_args__ = (
        UniqueConstraint("key", "merchant_id", name="uq_idempotency_key_merchant"),
    )

    key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="Client-supplied idempotency key (e.g. UUIDv4)"
    )
    merchant_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Merchant scoping identifier"
    )
    request_path: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="HTTP endpoint invoked"
    )
    request_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA-256 hash of canonicalized request payload"
    )
    status: Mapped[str] = mapped_column(
        String(30),
        default=IdempotencyStatus.PROCESSING.value,
        nullable=False,
        index=True
    )
    response_code: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="Cached HTTP status code returned upon completion"
    )
    response_headers: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Cached response headers (safe headers only)"
    )
    response_body: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Cached JSON payload returned upon completion"
    )
    locked_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        comment="Distributed lease lock timestamp for concurrency control"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        comment="TTL expiry for table pruning/retention policy"
    )

