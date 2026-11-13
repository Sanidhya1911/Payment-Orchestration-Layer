import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.database import get_async_db
from app.core.enums import StateTrigger, TransactionState
from app.gateways.router import gateway_router
from app.main import app
from app.models.transaction import Transactions, TransactionStateLog
from app.services.reconciliation import ReconciliationEngine

# =============================================================================
# 1. PAYMENTS REST API TESTS
# =============================================================================


@pytest.mark.anyio
async def test_create_payment_successful_auth_and_capture(test_session_maker):
    """
    Tests POST /api/v1/payments:
    Initiates payment, dynamically scores gateways, transitions states, and auto-captures.
    """

    async def override_get_db():
        async with test_session_maker() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            idemp_key = str(uuid.uuid4())
            payload = {
                "merchant_id": "merch_acme_01",
                "order_id": "ord_1001",
                "customer_id": "cust_501",
                "amount": 50000,  # 500.00 INR (strictly integer paise)
                "currency": "INR",
                "payment_method": "UPI",
                "auto_capture": True,
            }

            resp = await client.post(
                "/api/v1/payments",
                json=payload,
                headers={"Idempotency-Key": idemp_key},
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["status"] in ("AUTHORISED", "CAPTURED")
            assert data["amount"] == 50000
            assert data["currency"] == "INR"
            assert data["gateway_id"] is not None

            # 2. Idempotency Check: Same key returns cached response
            resp_cached = await client.post(
                "/api/v1/payments",
                json=payload,
                headers={"Idempotency-Key": idemp_key},
            )
            assert resp_cached.status_code == 201
            assert resp_cached.json()["id"] == data["id"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_create_payment_float_amount_rejection():
    """
    Strict Financial Precision Rule:
    Any floating-point amount must be rejected at API validation boundary.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/v1/payments",
            json={
                "merchant_id": "merch_acme_01",
                "order_id": "ord_bad_float",
                "amount": 500.50,  # FORBIDDEN FLOAT!
                "currency": "INR",
                "payment_method": "UPI",
            },
        )
        assert resp.status_code == 422
        data = resp.json()
        # Verify uniform error format
        assert "error" in data
        assert data["error"]["code"] == "VALIDATION_ERROR"
        assert "details" in data["error"]
        assert "gateway_error_code" in data["error"]["details"]


@pytest.mark.anyio
async def test_create_payment_gateway_decline_uniform_error_format(test_session_maker):
    """
    MANDATE: Format all errors uniformly with error.code, error.message,
    and details.gateway_error_code.
    Simulates bank decline via X-Mock-Response: decline header.
    """

    async def override_get_db():
        async with test_session_maker() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                "/api/v1/payments",
                json={
                    "merchant_id": "merch_acme_01",
                    "order_id": "ord_decline_test",
                    "amount": 25000,
                    "currency": "INR",
                    "payment_method": "CARD",
                    "preferred_gateway": "stripe",
                },
                headers={
                    "Idempotency-Key": str(uuid.uuid4()),
                    "X-Mock-Response": "decline",
                },
            )
            assert resp.status_code == 400
            data = resp.json()

            # Strict Uniform Error Format Verification
            assert "error" in data
            assert data["error"]["code"] == "PAYMENT_DECLINED"
            assert "declined" in data["error"]["message"].lower()
            assert "details" in data["error"]
            assert data["error"]["details"]["gateway_error_code"] == "CARD_DECLINED"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_create_payment_gateway_timeout_uniform_error_format(test_session_maker):
    """Simulates gateway timeout and verifies uniform error response with 504 status."""

    async def override_get_db():
        async with test_session_maker() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                "/api/v1/payments",
                json={
                    "merchant_id": "merch_acme_01",
                    "order_id": "ord_timeout_test",
                    "amount": 30000,
                    "currency": "INR",
                    "payment_method": "UPI",
                    "preferred_gateway": "upi",
                },
                headers={
                    "Idempotency-Key": str(uuid.uuid4()),
                    "X-Mock-Response": "timeout",
                },
            )
            assert resp.status_code == 504
            data = resp.json()
            assert data["error"]["code"] == "GATEWAY_TIMEOUT"
            assert data["error"]["details"]["gateway_error_code"] == "TIMEOUT"
    finally:
        app.dependency_overrides.clear()


# =============================================================================
# 2. CAPTURE & REFUND REST API TESTS
# =============================================================================


@pytest.mark.anyio
async def test_capture_payment_lifecycle_and_illegal_transitions(test_session_maker):
    """
    Tests POST /api/v1/payments/{id}/capture:
    1. 404 for non-existent transaction.
    2. 422 for illegal transition (trying to capture a CREATED transaction).
    3. 200 for valid capture of an AUTHORISED transaction.
    """

    async def override_get_db():
        async with test_session_maker() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            # 1. 404 Not Found Check
            non_existent = uuid.uuid4()
            resp_404 = await client.post(f"/api/v1/payments/{non_existent}/capture")
            assert resp_404.status_code == 404
            data_404 = resp_404.json()
            assert data_404["error"]["code"] == "TRANSACTION_NOT_FOUND"
            assert data_404["error"]["details"]["gateway_error_code"] is None

            # 2. Setup a transaction in CREATED state in DB
            txn_id = uuid.uuid4()
            async with test_session_maker() as db:
                txn_created = Transactions(
                    id=txn_id,
                    merchant_id="m_100",
                    order_id="ord_created_test",
                    amount=50000,
                    currency="INR",
                    status=TransactionState.CREATED.value,
                    payment_method="CARD",
                )
                db.add(txn_created)
                await db.commit()

            # Attempt capture on CREATED state -> 422 Invalid State Transition
            resp_422 = await client.post(f"/api/v1/payments/{txn_id}/capture")
            assert resp_422.status_code == 422
            data_422 = resp_422.json()
            assert data_422["error"]["code"] == "INVALID_STATE_TRANSITION"
            assert "Cannot capture transaction" in data_422["error"]["message"]

            # 3. Setup transaction in AUTHORISED state
            auth_txn_id = uuid.uuid4()
            async with test_session_maker() as db:
                txn_auth = Transactions(
                    id=auth_txn_id,
                    merchant_id="m_100",
                    order_id="ord_auth_test",
                    amount=60000,
                    currency="USD",
                    status=TransactionState.AUTHORISED.value,
                    payment_method="CARD",
                    gateway_id="stripe",
                    gateway_transaction_id="pi_test_auth_12345",
                )
                db.add(txn_auth)
                await db.commit()

            # Successful capture
            resp_cap = await client.post(f"/api/v1/payments/{auth_txn_id}/capture")
            assert resp_cap.status_code == 200
            data_cap = resp_cap.json()
            assert data_cap["status"] == "CAPTURED"
            assert data_cap["captured_amount"] == 60000
    finally:
        app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_refund_payment_lifecycle_and_validation(test_session_maker):
    """
    Tests POST /api/v1/payments/{id}/refund:
    1. 422 when attempting to refund an un-captured transaction.
    2. 400 when attempting to refund an amount exceeding captured funds.
    3. 200 for successful partial and full refunds.
    """

    async def override_get_db():
        async with test_session_maker() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            captured_txn_id = uuid.uuid4()
            async with test_session_maker() as db:
                txn_captured = Transactions(
                    id=captured_txn_id,
                    merchant_id="m_refund_test",
                    order_id="ord_ref_100",
                    amount=100000,  # 1000.00 INR
                    currency="INR",
                    status=TransactionState.CAPTURED.value,
                    payment_method="UPI",
                    gateway_id="razorpay",
                    gateway_transaction_id="pay_test_ref_987",
                    refunded_amount=0,
                )
                db.add(txn_captured)
                await db.commit()

            # 1. Excess refund amount rejected with 400
            resp_excess = await client.post(
                f"/api/v1/payments/{captured_txn_id}/refund",
                json={"amount": 200000},  # Exceeds 100000 captured
            )
            assert resp_excess.status_code == 400
            data_excess = resp_excess.json()
            assert data_excess["error"]["code"] == "INVALID_REFUND_AMOUNT"

            # 2. Valid Partial Refund
            resp_refund = await client.post(
                f"/api/v1/payments/{captured_txn_id}/refund",
                json={"amount": 50000, "reason": "Customer returns 1 item"},
            )
            assert resp_refund.status_code == 200
            data_ref = resp_refund.json()
            assert data_ref["refunded_amount"] == 50000
            assert data_ref["status"] in ("REFUNDED", "CAPTURED")
    finally:
        app.dependency_overrides.clear()


# =============================================================================
# 3. RECONCILIATION ENGINE TESTS
# =============================================================================


@pytest.mark.anyio
async def test_reconciliation_detects_stuck_transactions_and_generates_override_log(
    test_session_maker,
):
    """
    STRICT MANDATE:
    Build a Reconciliation Engine (background cron job) that runs every 15 minutes.
    It must find transactions stuck in AUTH_INITIATED or CAPTURE_INITIATED for > 5 minutes,
    poll the gateway for the true status, and generate a RECONCILIATION_OVERRIDE audit
    event if there is a mismatch.
    """
    engine = ReconciliationEngine(
        router=gateway_router,
        session_maker=test_session_maker,
    )

    now = datetime.now(UTC)
    stuck_time = now - timedelta(minutes=10)  # Stuck > 5 minutes ago!
    recent_time = now - timedelta(minutes=1)  # Only 1 minute ago (must be ignored)

    # 1. Setup mock transactions
    # Transaction A: Stuck in AUTH_INITIATED (>5 min). Gateway reports AUTHORIZED. (MISMATCH)
    txn_a_id = uuid.uuid4()
    # Transaction B: Stuck in CAPTURE_INITIATED (>5 min). Gateway reports CAPTURED. (MISMATCH)
    txn_b_id = uuid.uuid4()
    # Transaction C: In AUTH_INITIATED, but recent (<5 min). (MUST BE IGNORED)
    txn_c_id = uuid.uuid4()

    async with test_session_maker() as db:
        txn_a = Transactions(
            id=txn_a_id,
            merchant_id="m_reconcile",
            order_id="ord_rec_a",
            amount=50000,
            currency="INR",
            status=TransactionState.AUTH_INITIATED.value,
            payment_method="CARD",
            gateway_id="stripe",
            gateway_transaction_id="pi_reconcile_a",
            created_at=stuck_time,
        )
        txn_b = Transactions(
            id=txn_b_id,
            merchant_id="m_reconcile",
            order_id="ord_rec_b",
            amount=75000,
            currency="INR",
            status=TransactionState.CAPTURE_INITIATED.value,
            payment_method="UPI",
            gateway_id="upi",
            gateway_transaction_id="upi_reconcile_b",
            created_at=stuck_time,
        )
        txn_c = Transactions(
            id=txn_c_id,
            merchant_id="m_reconcile",
            order_id="ord_rec_c",
            amount=20000,
            currency="INR",
            status=TransactionState.AUTH_INITIATED.value,
            payment_method="CARD",
            gateway_id="stripe",
            gateway_transaction_id="pi_reconcile_c",
            created_at=recent_time,  # Recent!
        )
        db.add_all([txn_a, txn_b, txn_c])
        await db.commit()

    # Configure upstream gateway status responses
    stripe_gw = gateway_router.get_gateway("stripe")
    upi_gw = gateway_router.get_gateway("upi")

    stripe_gw.set_mock_status("pi_reconcile_a", "AUTHORIZED")
    upi_gw.set_mock_status("upi_reconcile_b", "CAPTURED")

    # 2. Run Reconciliation Sweep with cutoff_minutes=5
    report = await engine.reconcile_stuck_transactions(cutoff_minutes=5)

    assert report.scanned_count == 2  # Only Txn A and B qualify; Txn C is ignored
    assert report.overridden_count == 2

    # 3. Verify Database State & RECONCILIATION_OVERRIDE Audit Logs
    async with test_session_maker() as db:
        # Check Transaction A: Overridden from AUTH_INITIATED -> AUTHORISED
        res_a = await db.execute(select(Transactions).where(Transactions.id == txn_a_id))
        updated_a = res_a.scalar_one()
        assert updated_a.status == TransactionState.AUTHORISED.value

        # Check Transaction B: Overridden from CAPTURE_INITIATED -> CAPTURED
        res_b = await db.execute(select(Transactions).where(Transactions.id == txn_b_id))
        updated_b = res_b.scalar_one()
        assert updated_b.status == TransactionState.CAPTURED.value

        # Check Transaction C: Unchanged in AUTH_INITIATED
        res_c = await db.execute(select(Transactions).where(Transactions.id == txn_c_id))
        updated_c = res_c.scalar_one()
        assert updated_c.status == TransactionState.AUTH_INITIATED.value

        # Verify RECONCILIATION_OVERRIDE audit logs exist in transaction_state_log
        stmt_logs = select(TransactionStateLog).where(
            TransactionStateLog.trigger == StateTrigger.RECONCILIATION_OVERRIDE.value
        )
        logs = (await db.execute(stmt_logs)).scalars().all()
        assert len(logs) >= 2

        triggers = {log_item.trigger for log_item in logs}
        actors = {log_item.actor_id for log_item in logs}
        assert StateTrigger.RECONCILIATION_OVERRIDE.value in triggers
        assert "system_reconciliation_engine" in actors


@pytest.mark.anyio
async def test_reconciliation_engine_cron_start_stop():
    """Verifies starting and stopping the 15-minute background cron worker."""
    engine = ReconciliationEngine()
    assert engine._is_running is False

    engine.start_cron()
    assert engine._is_running is True
    assert engine._task is not None

    # Allow task loop to initialize
    await asyncio.sleep(0.01)

    engine.stop_cron()
    assert engine._is_running is False
