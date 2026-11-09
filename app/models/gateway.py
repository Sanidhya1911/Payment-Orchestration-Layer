import uuid
from datetime import datetime
from typing import List, Optional
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.core.enums import CircuitBreakerStatus, GatewayStatus
from app.models.base import TimestampMixin


class GatewayConfig(Base, TimestampMixin):
    """
    Registry of configured payment service providers (PSPs).
    Credentials are stored as external vault references, never raw secrets.
    """
    __tablename__ = "gateway_config"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)  # e.g., 'stripe_us', 'razorpay_in'
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    gateway_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(30),
        default=GatewayStatus.ACTIVE.value,
        nullable=False,
        index=True
    )
    api_base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    credentials_vault_ref: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="ARN or secret identifier pointing to KMS/HashiCorp Vault"
    )
    supported_currencies: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="List of ISO 4217 currency codes supported by gateway account"
    )
    supported_payment_methods: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="List of payment methods (CARD, UPI, etc.) supported"
    )
    rate_limit_tps: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=5000, nullable=False)
    config_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Relationships
    routes = relationship("GatewayRoutes", back_populates="gateway", cascade="all, delete-orphan")
    health_metrics = relationship("GatewayHealthMetrics", back_populates="gateway", cascade="all, delete-orphan")
    transactions = relationship("Transactions", back_populates="gateway")


class GatewayHealthMetrics(Base):
    """
    Time-windowed telemetry and circuit-breaker states per payment gateway.
    Essential for dynamic routing decisions and resilience.
    """
    __tablename__ = "gateway_health_metrics"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    gateway_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("gateway_config.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    p50_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    p95_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    p99_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    circuit_breaker_status: Mapped[str] = mapped_column(
        String(30),
        default=CircuitBreakerStatus.CLOSED.value,
        nullable=False,
        index=True
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True
    )

    gateway = relationship("GatewayConfig", back_populates="health_metrics")
