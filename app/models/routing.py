import uuid
from typing import Optional
from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.core.enums import RoutingAlgorithm
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class RoutingConfig(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Top-level routing configuration policies. Supports merchant-level overrides
    or global default routing behavior.
    """
    __tablename__ = "routing_config"

    merchant_id: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        index=True,
        comment="Merchant override identifier. NULL indicates default global platform routing."
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    algorithm: Mapped[str] = mapped_column(
        String(50),
        default=RoutingAlgorithm.PRIORITY.value,
        nullable=False
    )
    failover_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_retry_attempts: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    circuit_breaker_error_threshold_pct: Mapped[int] = mapped_column(
        Integer,
        default=20,
        nullable=False,
        comment="Percentage of gateway errors triggering route trip (0-100)"
    )
    config_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Relationships
    routes = relationship("GatewayRoutes", back_populates="routing_config", cascade="all, delete-orphan")


class GatewayRoutes(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Granular routing rules linking payments to target gateways based on
    currency, method, network tier, and min/max amount thresholds.
    """
    __tablename__ = "gateway_routes"

    routing_config_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("routing_config.id", ondelete="CASCADE"),
        nullable=True,
        index=True
    )
    gateway_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("gateway_config.id", ondelete="RESTRICT"),
        nullable=False,
        index=True
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
        index=True,
        comment="Priority sequence: lower number = higher execution priority"
    )
    weight: Mapped[int] = mapped_column(
        Integer,
        default=100,
        nullable=False,
        comment="Traffic split distribution weight (0-100)"
    )
    currency: Mapped[Optional[str]] = mapped_column(
        String(3),
        nullable=True,
        index=True,
        comment="ISO 4217 Currency match. NULL matches any currency."
    )
    payment_method: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        index=True,
        comment="Payment method match (CARD, UPI, etc.). NULL matches any."
    )
    card_network: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="VISA, MASTERCARD, RUPAY, AMEX. NULL matches any."
    )
    
    # Financial amount rule: STRICTLY BIGINT (storing in paise/cents)
    min_amount: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Minimum amount in paise/cents (BIGINT). NULL matches no lower bound."
    )
    max_amount: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Maximum amount in paise/cents (BIGINT). NULL matches no upper bound."
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    # Relationships
    routing_config = relationship("RoutingConfig", back_populates="routes")
    gateway = relationship("GatewayConfig", back_populates="routes")

