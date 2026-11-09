import pytest
from sqlalchemy import BigInteger, Float
from app.models import (
    Base,
    GatewayConfig,
    GatewayHealthMetrics,
    GatewayRoutes,
    IdempotencyKeys,
    ProcessedWebhookEvents,
    RoutingConfig,
    Transactions,
    TransactionStateLog,
)
from app.schemas.transaction import TransactionCreateRequest
from app.schemas.gateway import GatewayRouteCreate


def test_required_tables_present():
    """Verify all 8 mission-critical payment architecture tables are registered."""
    table_names = set(Base.metadata.tables.keys())
    required_tables = {
        "transactions",
        "transaction_state_log",
        "gateway_routes",
        "idempotency_keys",
        "processed_webhook_events",
        "gateway_health_metrics",
        "gateway_config",
        "routing_config",
    }
    missing = required_tables - table_names
    assert not missing, f"Missing required tables in SQLAlchemy metadata: {missing}"


def test_financial_amounts_strict_bigint_rule():
    """
    CRITICAL FINANCIAL ARCHITECTURE INVARIANT:
    ALL financial amounts MUST use BIGINT (storing paise/cents). NEVER floats.
    """
    metadata = Base.metadata

    # 1. Inspect transactions table
    txn_table = metadata.tables["transactions"]
    amount_col = txn_table.c.amount
    fee_col = txn_table.c.fee_amount
    tax_col = txn_table.c.tax_amount
    refund_col = txn_table.c.refunded_amount

    assert isinstance(amount_col.type, BigInteger), "transactions.amount must be BigInteger"
    assert isinstance(fee_col.type, BigInteger), "transactions.fee_amount must be BigInteger"
    assert isinstance(tax_col.type, BigInteger), "transactions.tax_amount must be BigInteger"
    assert isinstance(refund_col.type, BigInteger), "transactions.refunded_amount must be BigInteger"

    # 2. Inspect gateway_routes table
    route_table = metadata.tables["gateway_routes"]
    min_amount_col = route_table.c.min_amount
    max_amount_col = route_table.c.max_amount

    assert isinstance(min_amount_col.type, BigInteger), "gateway_routes.min_amount must be BigInteger"
    assert isinstance(max_amount_col.type, BigInteger), "gateway_routes.max_amount must be BigInteger"

    # 3. Global scan across ALL tables: Ensure no Float or Double precision columns exist for amounts
    for table in metadata.tables.values():
        for col in table.columns:
            if "amount" in col.name.lower():
                assert not isinstance(col.type, Float), (
                    f"Forbidden Float detected in financial column {table.name}.{col.name}"
                )
                assert isinstance(col.type, BigInteger), (
                    f"Financial column {table.name}.{col.name} must be BigInteger (paise/cents)"
                )


def test_processed_webhook_events_composite_pk():
    """
    STRICT REQUIREMENT:
    Ensure processed_webhook_events uses a composite primary key of (gateway, event_id).
    """
    webhook_table = Base.metadata.tables["processed_webhook_events"]
    pk_column_names = [col.name for col in webhook_table.primary_key.columns]

    assert len(pk_column_names) == 2, (
        f"Expected 2 columns in composite PK, found {len(pk_column_names)}: {pk_column_names}"
    )
    assert set(pk_column_names) == {"gateway", "event_id"}, (
        f"Composite PK must be exactly ('gateway', 'event_id'), got {pk_column_names}"
    )


def test_transaction_state_log_immutability():
    """
    Verify transaction_state_log is strictly an append-only audit trail:
    - Must NOT have an 'updated_at' column.
    - Must have foreign key linking to transactions.id.
    - Must have a monotonically increasing 64-bit ID (BigInteger).
    """
    log_table = Base.metadata.tables["transaction_state_log"]
    column_names = [col.name for col in log_table.columns]

    assert "updated_at" not in column_names, "transaction_state_log must be immutable: no updated_at column allowed"
    assert "created_at" in column_names, "transaction_state_log must have created_at timestamp"
    assert isinstance(log_table.c.id.type, BigInteger), "transaction_state_log.id must be BigInteger"

    # Verify foreign key to transactions.id
    fk_targets = [fk.target_fullname for fk in log_table.foreign_keys]
    assert "transactions.id" in fk_targets, "transaction_state_log must reference transactions.id"


def test_pydantic_schema_rejects_floating_point_amounts():
    """Verify API schema level enforcement rejects floats for amounts."""
    # Attempting to supply a float (e.g. 500.50) MUST raise ValidationError
    with pytest.raises(Exception):
        TransactionCreateRequest(
            merchant_id="m_123",
            order_id="ord_999",
            amount=500.50,  # Float: Invalid!
            currency="INR",
            payment_method="UPI",
        )

    # Valid integer paise (50050 = 500.50 INR)
    valid_req = TransactionCreateRequest(
        merchant_id="m_123",
        order_id="ord_999",
        amount=50050,  # Integer paise: Valid!
        currency="INR",
        payment_method="UPI",
    )
    assert valid_req.amount == 50050


def test_fastapi_app_routes():
    """Verify FastAPI instance registers all required routes and generates valid OpenAPI schema."""
    from app.main import app

    openapi_schema = app.openapi()
    paths = set(openapi_schema["paths"].keys())
    assert "/health" in paths
    assert "/api/v1/transactions" in paths
    assert "/api/v1/transactions/{transaction_id}" in paths
    assert "/api/v1/webhooks/{gateway}" in paths
    assert "/api/v1/gateways" in paths
    assert "/api/v1/gateways/routes" in paths
    assert "/api/v1/gateways/routing-configs" in paths
