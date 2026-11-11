import uuid
from datetime import datetime
from typing import List, Optional
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.core.enums import TransactionState, TransactionStatus
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class Transactions(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Core transaction ledger entry.
    STRICT FINANCIAL ARCHITECTURE RULE:
    All monetary amounts MUST be stored as BIGINT in minor units (paise, cents, pence).
    NEVER store floating-point numbers for currency representations.
    """
    __tablename__ = "transactions"

    __table_args__ = (
        Index("ix_transactions_merchant_created", "merchant_id", "created_at"),
        Index("ix_transactions_gateway_txn", "gateway_id", "gateway_transaction_id"),
    )

    merchant_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Tenant / Merchant account ID"
    )
    customer_id: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        index=True,
        comment="End-user customer identifier"
    )
    order_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Merchant order/cart reference ID"
    )

    # =========================================================================
    # FINANCIAL AMOUNTS - STRICT BIGINT ENFORCEMENT (PAISE / CENTS / MINOR UNITS)
    # =========================================================================
    amount: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Authorized transaction amount in minor currency units (paise/cents). BIGINT only."
    )
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        index=True,
        comment="ISO 4217 3-letter currency code (e.g., INR, USD, EUR)"
    )
    fee_amount: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
        comment="PSP / Gateway fee charged in minor currency units (paise/cents)"
    )
    tax_amount: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
        comment="Tax component (e.g., GST/VAT) in minor currency units (paise/cents)"
    )
    refunded_amount: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
        comment="Cumulative refunded amount in minor currency units (paise/cents)"
    )

    status: Mapped[str] = mapped_column(
        String(50),
        default=TransactionState.CREATED.value,
        nullable=False,
        index=True
    )
    payment_method: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="CARD, UPI, NETBANKING, WALLET, etc."
    )
    payment_method_details: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Tokenized card last4, network, UPI VPA handle, or bank code"
    )
    gateway_id: Mapped[Optional[str]] = mapped_column(
        String(50),
        ForeignKey("gateway_config.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Target PSP that routed and processed this transaction"
    )
    gateway_transaction_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        comment="PSP's internal reference ID (e.g. pi_..., pay_...)"
    )
    gateway_reference_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        comment="Banking settlement ARN / RRN / Authorization Code"
    )
    error_code: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        comment="Normalized business or gateway error code"
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable or gateway failure explanation"
    )
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
        comment="Arbitrary contextual merchant-provided metadata"
    )

    # Relationships
    gateway = relationship("GatewayConfig", back_populates="transactions")
    state_logs = relationship(
        "TransactionStateLog",
        back_populates="transaction",
        cascade="all, delete-orphan",
        order_by="TransactionStateLog.id"
    )


class TransactionStateLog(Base):
    """
    Immutable, append-only audit trail recording every state transition.
    Strictly zero in-place updates. Every status shift is a new row.
    """
    __tablename__ = "transaction_state_log"

    __table_args__ = (
        Index("ix_state_log_txn_created", "transaction_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        comment="Sequential 64-bit monotonically increasing log ID"
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    from_state: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="Previous transaction status (NULL for initial creation)"
    )
    to_state: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="New transaction status reached"
    )
    trigger: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Cause of transition: API_REQUEST, GATEWAY_WEBHOOK, RECONCILIATION_JOB, etc."
    )
    actor_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Executing actor: service identifier, worker name, or authenticated admin"
    )
    gateway_payload: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Sanitized gateway response or webhook payload captured during transition"
    )
    transition_metadata: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        nullable=True,
        comment="Diagnostics, latency ms, retry count, or routing reasons"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
        comment="Immutable timestamp of state change"
    )

    # Relationships
    transaction = relationship("Transactions", back_populates="state_logs")

