"""0001_initial_payment_schema

Revision ID: 0001_initial_payment_schema
Revises:
Create Date: 2026-11-10 01:58:00.000000

Architectural Mandates:
- ALL financial amounts use BIGINT (storing paise/cents/minor units), NEVER floats.
- transaction_state_log is strictly append-only (immutable audit trail).
- processed_webhook_events uses composite primary key of (gateway, event_id).
- Full foreign keys and indexing for ultra-high throughput and sub-millisecond lookup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial_payment_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. GATEWAY_CONFIG
    # -------------------------------------------------------------------------
    op.create_table(
        "gateway_config",
        sa.Column("id", sa.String(length=50), nullable=False, primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("gateway_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="ACTIVE", nullable=False),
        sa.Column("api_base_url", sa.String(length=255), nullable=False),
        sa.Column(
            "credentials_vault_ref",
            sa.String(length=255),
            nullable=False,
            comment="Secret reference in external vault",
        ),
        sa.Column(
            "supported_currencies",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "supported_payment_methods",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("rate_limit_tps", sa.Integer(), server_default="100", nullable=False),
        sa.Column("timeout_ms", sa.Integer(), server_default="5000", nullable=False),
        sa.Column("config_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_gateway_config_gateway_type", "gateway_config", ["gateway_type"])
    op.create_index("ix_gateway_config_status", "gateway_config", ["status"])
    op.create_index("ix_gateway_config_created_at", "gateway_config", ["created_at"])

    # -------------------------------------------------------------------------
    # 2. ROUTING_CONFIG
    # -------------------------------------------------------------------------
    op.create_table(
        "routing_config",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True),
        sa.Column(
            "merchant_id",
            sa.String(length=100),
            nullable=True,
            comment="NULL represents global platform default",
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("algorithm", sa.String(length=50), server_default="PRIORITY", nullable=False),
        sa.Column(
            "failover_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("max_retry_attempts", sa.Integer(), server_default="2", nullable=False),
        sa.Column(
            "circuit_breaker_error_threshold_pct",
            sa.Integer(),
            server_default="20",
            nullable=False,
        ),
        sa.Column("config_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_routing_config_merchant_id", "routing_config", ["merchant_id"])
    op.create_index("ix_routing_config_is_active", "routing_config", ["is_active"])
    op.create_index("ix_routing_config_created_at", "routing_config", ["created_at"])

    # -------------------------------------------------------------------------
    # 3. IDEMPOTENCY_KEYS
    # -------------------------------------------------------------------------
    op.create_table(
        "idempotency_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("merchant_id", sa.String(length=100), nullable=False),
        sa.Column("request_path", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="PROCESSING", nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("response_headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("key", "merchant_id", name="uq_idempotency_key_merchant"),
    )
    op.create_index("ix_idempotency_keys_key", "idempotency_keys", ["key"])
    op.create_index("ix_idempotency_keys_merchant_id", "idempotency_keys", ["merchant_id"])
    op.create_index("ix_idempotency_keys_status", "idempotency_keys", ["status"])
    op.create_index("ix_idempotency_keys_locked_until", "idempotency_keys", ["locked_until"])
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])

    # -------------------------------------------------------------------------
    # 4. PROCESSED_WEBHOOK_EVENTS (COMPOSITE PRIMARY KEY: gateway, event_id)
    # -------------------------------------------------------------------------
    op.create_table(
        "processed_webhook_events",
        sa.Column("gateway", sa.String(length=50), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="RECEIVED", nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("gateway", "event_id", name="pk_processed_webhook_events"),
    )
    op.create_index(
        "ix_processed_webhook_events_event_type",
        "processed_webhook_events",
        ["event_type"],
    )
    op.create_index("ix_processed_webhook_events_status", "processed_webhook_events", ["status"])
    op.create_index(
        "ix_processed_webhook_events_received_at",
        "processed_webhook_events",
        ["received_at"],
    )

    # -------------------------------------------------------------------------
    # 5. GATEWAY_HEALTH_METRICS
    # -------------------------------------------------------------------------
    op.create_table(
        "gateway_health_metrics",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True),
        sa.Column("gateway_id", sa.String(length=50), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_requests", sa.Integer(), server_default="0", nullable=False),
        sa.Column("successful_requests", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_requests", sa.Integer(), server_default="0", nullable=False),
        sa.Column("p50_latency_ms", sa.Integer(), nullable=True),
        sa.Column("p95_latency_ms", sa.Integer(), nullable=True),
        sa.Column("p99_latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "circuit_breaker_status",
            sa.String(length=30),
            server_default="CLOSED",
            nullable=False,
        ),
        sa.Column(
            "last_heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["gateway_id"], ["gateway_config.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_gateway_health_metrics_gateway_id", "gateway_health_metrics", ["gateway_id"]
    )
    op.create_index(
        "ix_gateway_health_metrics_circuit_breaker_status",
        "gateway_health_metrics",
        ["circuit_breaker_status"],
    )
    op.create_index(
        "ix_gateway_health_metrics_created_at", "gateway_health_metrics", ["created_at"]
    )

    # -------------------------------------------------------------------------
    # 6. GATEWAY_ROUTES (ALL amounts BIGINT paise/cents)
    # -------------------------------------------------------------------------
    op.create_table(
        "gateway_routes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True),
        sa.Column("routing_config_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("gateway_id", sa.String(length=50), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="1", nullable=False),
        sa.Column("weight", sa.Integer(), server_default="100", nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("payment_method", sa.String(length=50), nullable=True),
        sa.Column("card_network", sa.String(length=50), nullable=True),
        sa.Column(
            "min_amount",
            sa.BigInteger(),
            nullable=True,
            comment="Min amount in paise/cents (BIGINT)",
        ),
        sa.Column(
            "max_amount",
            sa.BigInteger(),
            nullable=True,
            comment="Max amount in paise/cents (BIGINT)",
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["routing_config_id"], ["routing_config.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["gateway_id"], ["gateway_config.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_gateway_routes_routing_config_id", "gateway_routes", ["routing_config_id"])
    op.create_index("ix_gateway_routes_gateway_id", "gateway_routes", ["gateway_id"])
    op.create_index("ix_gateway_routes_priority", "gateway_routes", ["priority"])
    op.create_index("ix_gateway_routes_currency", "gateway_routes", ["currency"])
    op.create_index("ix_gateway_routes_payment_method", "gateway_routes", ["payment_method"])
    op.create_index("ix_gateway_routes_is_active", "gateway_routes", ["is_active"])
    op.create_index("ix_gateway_routes_created_at", "gateway_routes", ["created_at"])

    # -------------------------------------------------------------------------
    # 7. TRANSACTIONS (ALL amounts BIGINT paise/cents)
    # -------------------------------------------------------------------------
    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True),
        sa.Column("merchant_id", sa.String(length=100), nullable=False),
        sa.Column("customer_id", sa.String(length=100), nullable=True),
        sa.Column("order_id", sa.String(length=100), nullable=False),
        sa.Column(
            "amount",
            sa.BigInteger(),
            nullable=False,
            comment="Strict BIGINT in minor units (paise/cents)",
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "fee_amount",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
            comment="Strict BIGINT fee in minor units",
        ),
        sa.Column(
            "tax_amount",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
            comment="Strict BIGINT tax in minor units",
        ),
        sa.Column(
            "refunded_amount",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
            comment="Strict BIGINT refund in minor units",
        ),
        sa.Column("status", sa.String(length=50), server_default="CREATED", nullable=False),
        sa.Column("payment_method", sa.String(length=50), nullable=False),
        sa.Column(
            "payment_method_details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("gateway_id", sa.String(length=50), nullable=True),
        sa.Column("gateway_transaction_id", sa.String(length=255), nullable=True),
        sa.Column("gateway_reference_id", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["gateway_id"], ["gateway_config.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_transactions_merchant_id", "transactions", ["merchant_id"])
    op.create_index("ix_transactions_customer_id", "transactions", ["customer_id"])
    op.create_index("ix_transactions_order_id", "transactions", ["order_id"])
    op.create_index("ix_transactions_currency", "transactions", ["currency"])
    op.create_index("ix_transactions_status", "transactions", ["status"])
    op.create_index("ix_transactions_payment_method", "transactions", ["payment_method"])
    op.create_index("ix_transactions_gateway_id", "transactions", ["gateway_id"])
    op.create_index(
        "ix_transactions_gateway_transaction_id",
        "transactions",
        ["gateway_transaction_id"],
    )
    op.create_index(
        "ix_transactions_gateway_reference_id", "transactions", ["gateway_reference_id"]
    )
    op.create_index("ix_transactions_created_at", "transactions", ["created_at"])
    op.create_index(
        "ix_transactions_merchant_created",
        "transactions",
        ["merchant_id", "created_at"],
    )
    op.create_index(
        "ix_transactions_gateway_txn",
        "transactions",
        ["gateway_id", "gateway_transaction_id"],
    )

    # -------------------------------------------------------------------------
    # 8. TRANSACTION_STATE_LOG (IMMUTABLE AUDIT TRAIL - NO UPDATED_AT)
    # -------------------------------------------------------------------------
    op.create_table(
        "transaction_state_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, primary_key=True),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_state", sa.String(length=50), nullable=True),
        sa.Column("to_state", sa.String(length=50), nullable=False),
        sa.Column("trigger", sa.String(length=50), nullable=False),
        sa.Column("actor_id", sa.String(length=100), nullable=False),
        sa.Column("gateway_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "transition_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_transaction_state_log_transaction_id",
        "transaction_state_log",
        ["transaction_id"],
    )
    op.create_index("ix_transaction_state_log_to_state", "transaction_state_log", ["to_state"])
    op.create_index("ix_transaction_state_log_created_at", "transaction_state_log", ["created_at"])
    op.create_index(
        "ix_state_log_txn_created",
        "transaction_state_log",
        ["transaction_id", "created_at"],
    )


def downgrade() -> None:
    # Drop in exact reverse foreign key dependency order
    op.drop_table("transaction_state_log")
    op.drop_table("transactions")
    op.drop_table("gateway_routes")
    op.drop_table("gateway_health_metrics")
    op.drop_table("processed_webhook_events")
    op.drop_table("idempotency_keys")
    op.drop_table("routing_config")
    op.drop_table("gateway_config")
