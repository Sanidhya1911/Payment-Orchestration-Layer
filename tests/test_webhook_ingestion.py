import hashlib
import hmac
import json
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.database import get_async_db
from app.core.enums import WebhookProcessingStatus
from app.main import app
from app.models.webhook import ProcessedWebhookEvents
from app.services.webhook import (
    InvalidWebhookSignatureException,
    MissingWebhookSignatureException,
    WebhookIngestionService,
    WebhookProcessingException,
    generate_webhook_signature,
    timing_safe_equal,
    timingSafeEqual,
    verify_hmac_signature,
)

# =============================================================================
# 1. TIMING-SAFE EQUALITY & SECURITY TESTS
# =============================================================================


def test_timing_safe_equal_constant_time_comparison():
    """
    STRICT MANDATE:
    Use Python's hmac and hashlib with timingSafeEqual to prevent timing attacks.
    """
    # 1. Matching string comparison
    sig1 = "a1b2c3d4e5f67890abcdef1234567890abcdef1234567890abcdef1234567890"
    sig2 = "a1b2c3d4e5f67890abcdef1234567890abcdef1234567890abcdef1234567890"
    assert timingSafeEqual(sig1, sig2) is True
    assert timing_safe_equal(sig1, sig2) is True

    # 2. Mismatched string comparison (different single character)
    tampered_sig = "a1b2c3d4e5f67890abcdef1234567890abcdef1234567890abcdef1234567891"
    assert timingSafeEqual(sig1, tampered_sig) is False

    # 3. Differing length strings should not raise exceptions
    short_sig = "a1b2c3"
    assert timingSafeEqual(sig1, short_sig) is False

    # 4. Raw bytes comparison
    b1 = b"secure_hmac_token_value_32_bytes!"
    b2 = b"secure_hmac_token_value_32_bytes!"
    b_tampered = b"secure_hmac_token_value_32_bytes?"
    assert timingSafeEqual(b1, b2) is True
    assert timingSafeEqual(b1, b_tampered) is False


# =============================================================================
# 2. HMAC-SHA256 SIGNATURE VERIFICATION FOR 4 GATEWAYS
# =============================================================================


@pytest.mark.parametrize(
    "gateway,header_name",
    [
        ("razorpay", "X-Razorpay-Signature"),
        ("payu", "X-PayU-Signature"),
        ("upi", "X-UPI-Signature"),
    ],
)
def test_hmac_sha256_verification_standard_gateways(gateway: str, header_name: str):
    """
    Verifies HMAC-SHA256 signature verification for Razorpay, PayU, and UPI.
    Validates correct calculation, timingSafeEqual comparison, and rejection of tampered bodies.
    """
    raw_body = json.dumps({"event_id": f"evt_{gateway}_123", "amount": 10000}).encode("utf-8")
    secret = "test_shared_secret_key_32_chars_ok"

    # Compute valid signature
    computed_sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    headers = {header_name: computed_sig}

    # 1. Valid signature passes verification
    assert verify_hmac_signature(gateway, raw_body, headers, secret=secret) is True

    # 2. Case-insensitive header name matching
    lowercase_headers = {header_name.lower(): computed_sig}
    assert verify_hmac_signature(gateway, raw_body, lowercase_headers, secret=secret) is True

    # 3. Tampered payload with valid original signature raises InvalidWebhookSignatureException
    tampered_body = json.dumps({"event_id": f"evt_{gateway}_123", "amount": 99999}).encode("utf-8")
    with pytest.raises(InvalidWebhookSignatureException) as exc_info:
        verify_hmac_signature(gateway, tampered_body, headers, secret=secret)
    assert exc_info.value.gateway == gateway

    # 4. Corrupted signature string raises InvalidWebhookSignatureException
    corrupted_headers = {header_name: "deadbeef" * 8}
    with pytest.raises(InvalidWebhookSignatureException):
        verify_hmac_signature(gateway, raw_body, corrupted_headers, secret=secret)

    # 5. Missing signature header raises MissingWebhookSignatureException
    with pytest.raises(MissingWebhookSignatureException):
        verify_hmac_signature(gateway, raw_body, {}, secret=secret)


def test_hmac_sha256_stripe_signature_verification():
    """
    Verifies Stripe's specific timestamped HMAC-SHA256 signature scheme:
    Header: Stripe-Signature: t={timestamp},v1={hash}
    Signed data: {timestamp}.{raw_body}
    """
    gateway = "stripe"
    raw_body = json.dumps({"id": "evt_stripe_999", "type": "payment_intent.succeeded"}).encode(
        "utf-8"
    )
    secret = "whsec_stripe_test_secret_32_bytes_ok"
    timestamp = int(time.time())

    # Compute valid Stripe signature
    signed_payload = f"{timestamp}.{raw_body.decode('utf-8')}".encode()
    computed_sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    valid_header_val = f"t={timestamp},v1={computed_sig}"

    headers = {"Stripe-Signature": valid_header_val}

    # 1. Valid Stripe signature passes
    assert verify_hmac_signature(gateway, raw_body, headers, secret=secret) is True

    # 2. Multiple signatures in header (e.g. rolling secrets v1=old,v1=new)
    multi_sig_header = f"t={timestamp},v1=fake_old_hash,v1={computed_sig}"
    assert (
        verify_hmac_signature(
            gateway, raw_body, {"Stripe-Signature": multi_sig_header}, secret=secret
        )
        is True
    )

    # 3. Tampered timestamp fails
    tampered_ts_header = f"t={timestamp + 100},v1={computed_sig}"
    with pytest.raises(InvalidWebhookSignatureException):
        verify_hmac_signature(
            gateway, raw_body, {"Stripe-Signature": tampered_ts_header}, secret=secret
        )

    # 4. Missing signature header
    with pytest.raises(MissingWebhookSignatureException):
        verify_hmac_signature(gateway, raw_body, {}, secret=secret)


def test_generate_webhook_signature_helper():
    """Verifies the utility helper generates verifiable signatures for all 4 gateways."""
    for gw in ("razorpay", "stripe", "payu", "upi"):
        body = b'{"status":"ok","event_id":"evt_test"}'
        hdr_name, hdr_val = generate_webhook_signature(gw, body)
        assert hdr_name is not None
        assert hdr_val is not None
        # Must successfully verify against itself
        assert verify_hmac_signature(gw, body, {hdr_name: hdr_val}) is True


# =============================================================================
# 3. DEDUPLICATION TESTS (COMPOSITE PRIMARY KEY: gateway, event_id)
# =============================================================================


@pytest.mark.anyio
async def test_deduplication_returns_http_200_immediately_for_duplicate_event(
    test_session_maker,
):
    """
    STRICT MANDATE:
    Implement deduplication: Extract the event_id, check the processed_webhook_events
    table, and return HTTP 200 immediately if it's a duplicate.
    """
    service = WebhookIngestionService()
    event_id = f"evt_{uuid.uuid4()}"
    gateway = "razorpay"
    payload = {"event_id": event_id, "amount": 25000, "status": "authorized"}
    raw_body = json.dumps(payload).encode("utf-8")
    hdr_name, hdr_val = generate_webhook_signature(gateway, raw_body)
    headers = {hdr_name: hdr_val}

    processed_calls = 0

    async def mock_handler(gw, ev_type, pld, db):
        nonlocal processed_calls
        processed_calls += 1

    # First ingestion attempt: should execute handler and mark PROCESSED
    async with test_session_maker() as db:
        res1 = await service.ingest_webhook(
            gateway=gateway,
            raw_body=raw_body,
            headers=headers,
            db=db,
            custom_handler=mock_handler,
        )
        assert res1["status"] == "PROCESSED"
        assert res1["event_id"] == event_id
        assert processed_calls == 1

        # Check DB record
        stmt = select(ProcessedWebhookEvents).where(
            ProcessedWebhookEvents.gateway == gateway,
            ProcessedWebhookEvents.event_id == event_id,
        )
        record = (await db.execute(stmt)).scalar_one()
        assert record.status == "PROCESSED"
        assert record.retry_count == 0

    # Second duplicate ingestion attempt: must return HTTP 200 duplicate_ignored immediately!
    async with test_session_maker() as db:
        res2 = await service.ingest_webhook(
            gateway=gateway,
            raw_body=raw_body,
            headers=headers,
            db=db,
            custom_handler=mock_handler,
        )
        assert res2["status"] == "duplicate_ignored"
        assert res2["event_id"] == event_id
        assert "Duplicate webhook event already processed" in res2["message"]
        # Handler must NOT have been called again!
        assert processed_calls == 1


@pytest.mark.anyio
async def test_composite_primary_key_allows_same_event_id_across_different_gateways(
    test_session_maker,
):
    """
    Ensures composite primary key (gateway, event_id) isolates event spaces:
    'stripe' with event 'evt_100' and 'razorpay' with event 'evt_100' are distinct records.
    """
    service = WebhookIngestionService()
    shared_event_id = f"evt_shared_{uuid.uuid4()}"

    for gw in ("stripe", "razorpay", "upi"):
        body = json.dumps({"event_id": shared_event_id, "gateway": gw}).encode("utf-8")
        h_name, h_val = generate_webhook_signature(gw, body)
        async with test_session_maker() as db:
            res = await service.ingest_webhook(
                gateway=gw,
                raw_body=body,
                headers={h_name: h_val},
                db=db,
            )
            assert res["status"] == "PROCESSED"
            assert res["gateway"] == gw
            assert res["event_id"] == shared_event_id

    # Verify 3 distinct rows exist in processed_webhook_events
    async with test_session_maker() as db:
        stmt = select(ProcessedWebhookEvents).where(
            ProcessedWebhookEvents.event_id == shared_event_id
        )
        rows = (await db.execute(stmt)).scalars().all()
        assert len(rows) == 3
        gateways_found = {r.gateway for r in rows}
        assert gateways_found == {"stripe", "razorpay", "upi"}


# =============================================================================
# 4. DEAD LETTER QUEUE (DLQ) PATTERN TESTS
# =============================================================================


@pytest.mark.anyio
async def test_dlq_pattern_transitions_status_to_dlq_after_three_failures(
    test_session_maker,
):
    """
    STRICT MANDATE:
    Add a Dead Letter Queue (DLQ) pattern: If a webhook fails processing 3 times,
    move its status to 'DLQ' so it isn't lost.
    """
    service = WebhookIngestionService()
    event_id = f"evt_dlq_test_{uuid.uuid4()}"
    gateway = "upi"
    payload = {"event_id": event_id, "amount": 50000}
    raw_body = json.dumps(payload).encode("utf-8")
    h_name, h_val = generate_webhook_signature(gateway, raw_body)
    headers = {h_name: h_val}

    async def always_failing_handler(gw, ev_type, pld, db):
        raise RuntimeError("Downstream database connection dropped")

    # -------------------------------------------------------------------------
    # Attempt 1: First failure -> status = FAILED, retry_count = 1
    # -------------------------------------------------------------------------
    async with test_session_maker() as db:
        with pytest.raises(WebhookProcessingException) as exc:
            await service.ingest_webhook(
                gateway=gateway,
                raw_body=raw_body,
                headers=headers,
                db=db,
                custom_handler=always_failing_handler,
            )
        assert "attempt 1/3" in str(exc.value)

        # Inspect database row
        stmt = select(ProcessedWebhookEvents).where(
            ProcessedWebhookEvents.gateway == gateway,
            ProcessedWebhookEvents.event_id == event_id,
        )
        row = (await db.execute(stmt)).scalar_one()
        assert row.status == WebhookProcessingStatus.FAILED.value
        assert row.retry_count == 1
        assert "Downstream database connection dropped" in row.processing_error

    # -------------------------------------------------------------------------
    # Attempt 2: Second failure -> status = FAILED, retry_count = 2
    # -------------------------------------------------------------------------
    async with test_session_maker() as db:
        with pytest.raises(WebhookProcessingException) as exc:
            await service.ingest_webhook(
                gateway=gateway,
                raw_body=raw_body,
                headers=headers,
                db=db,
                custom_handler=always_failing_handler,
            )
        assert "attempt 2/3" in str(exc.value)

        stmt = select(ProcessedWebhookEvents).where(
            ProcessedWebhookEvents.gateway == gateway,
            ProcessedWebhookEvents.event_id == event_id,
        )
        row = (await db.execute(stmt)).scalar_one()
        assert row.status == WebhookProcessingStatus.FAILED.value
        assert row.retry_count == 2

    # -------------------------------------------------------------------------
    # Attempt 3: Third failure -> transitions to DLQ!
    # -------------------------------------------------------------------------
    async with test_session_maker() as db:
        # On 3rd failure, service routes to DLQ and returns DLQ confirmation
        res3 = await service.ingest_webhook(
            gateway=gateway,
            raw_body=raw_body,
            headers=headers,
            db=db,
            custom_handler=always_failing_handler,
        )
        assert res3["status"] == WebhookProcessingStatus.DLQ.value
        assert res3["retry_count"] == 3
        assert "DLQ" in res3["status"]

        # Verify persistent row state in database
        stmt = select(ProcessedWebhookEvents).where(
            ProcessedWebhookEvents.gateway == gateway,
            ProcessedWebhookEvents.event_id == event_id,
        )
        row = (await db.execute(stmt)).scalar_one()
        assert row.status == WebhookProcessingStatus.DLQ.value
        assert row.retry_count == 3
        assert "Downstream database connection dropped" in row.processing_error
        assert row.raw_payload is not None  # Preserved so it isn't lost!

    # -------------------------------------------------------------------------
    # Subsequent calls when already in DLQ: returns HTTP 200 dlq_acknowledged immediately!
    # -------------------------------------------------------------------------
    async with test_session_maker() as db:
        res4 = await service.ingest_webhook(
            gateway=gateway,
            raw_body=raw_body,
            headers=headers,
            db=db,
            custom_handler=always_failing_handler,
        )
        assert res4["status"] == "dlq_acknowledged"
        assert res4["retry_count"] == 3


@pytest.mark.anyio
async def test_dlq_inspection_and_successful_replay(test_session_maker):
    """
    Verifies operational management of DLQ events:
    1. Query DLQ records via get_dlq_events.
    2. Successfully replay a dead-lettered event once underlying issue is resolved.
    """
    service = WebhookIngestionService()
    event_id = f"evt_replay_{uuid.uuid4()}"
    gateway = "payu"
    raw_body = json.dumps({"event_id": event_id, "amount": 15000}).encode("utf-8")
    h_name, h_val = generate_webhook_signature(gateway, raw_body)

    # Put event into DLQ by failing 3 times
    async def failing_handler(gw, ev_type, pld, db):
        raise ValueError("Simulated network timeout")

    for _ in range(3):
        async with test_session_maker() as db:
            try:
                await service.ingest_webhook(
                    gateway=gateway,
                    raw_body=raw_body,
                    headers={h_name: h_val},
                    db=db,
                    custom_handler=failing_handler,
                )
            except WebhookProcessingException:
                pass

    # 1. Query DLQ events
    async with test_session_maker() as db:
        dlq_list = await service.get_dlq_events(db=db, gateway=gateway)
        dlq_event_ids = [e.event_id for e in dlq_list]
        assert event_id in dlq_event_ids

    # 2. Replay event with healthy handler
    async def healthy_handler(gw, ev_type, pld, db):
        return  # Successfully processed

    async with test_session_maker() as db:
        replayed = await service.replay_dlq_event(
            gateway=gateway,
            event_id=event_id,
            db=db,
            custom_handler=healthy_handler,
        )
        assert replayed.status == WebhookProcessingStatus.PROCESSED.value
        assert replayed.processing_error is None


# =============================================================================
# 5. FASTAPI HTTP API END-TO-END TESTS
# =============================================================================


@pytest.mark.anyio
async def test_fastapi_webhook_ingest_endpoint_valid_and_duplicate(test_session_maker):
    """Tests the /api/v1/webhooks/{gateway} FastAPI endpoint over ASGI."""

    async def override_get_db():
        async with test_session_maker() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            event_id = f"evt_api_{uuid.uuid4()}"
            gateway = "razorpay"
            payload = {
                "event_id": event_id,
                "event_type": "payment.captured",
                "amount": 40000,
            }
            raw_body = json.dumps(payload).encode("utf-8")
            h_name, h_val = generate_webhook_signature(gateway, raw_body)

            # 1. Successful first ingestion (HTTP 200)
            resp1 = await client.post(
                f"/api/v1/webhooks/{gateway}",
                content=raw_body,
                headers={"Content-Type": "application/json", h_name: h_val},
            )
            assert resp1.status_code == 200
            data1 = resp1.json()
            assert data1["status"] == "PROCESSED"
            assert data1["event_id"] == event_id

            # 2. Duplicate ingestion returns HTTP 200 immediately
            resp2 = await client.post(
                f"/api/v1/webhooks/{gateway}",
                content=raw_body,
                headers={"Content-Type": "application/json", h_name: h_val},
            )
            assert resp2.status_code == 200
            data2 = resp2.json()
            assert data2["status"] == "duplicate_ignored"
            assert data2["event_id"] == event_id
    finally:
        app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_fastapi_webhook_ingest_endpoint_signature_failures(test_session_maker):
    """Tests 401 Unauthorized responses for invalid or missing signatures on FastAPI endpoint."""

    async def override_get_db():
        async with test_session_maker() as session:
            yield session

    app.dependency_overrides[get_async_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            raw_body = json.dumps({"event_id": "evt_bad_sig", "amount": 10000}).encode("utf-8")

            # 1. Missing signature -> 401 Unauthorized
            resp_missing = await client.post(
                "/api/v1/webhooks/razorpay",
                content=raw_body,
                headers={"Content-Type": "application/json"},
            )
            assert resp_missing.status_code == 401
            assert "Missing webhook signature" in resp_missing.json()["detail"]

            # 2. Corrupted signature -> 401 Unauthorized
            resp_invalid = await client.post(
                "/api/v1/webhooks/razorpay",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Razorpay-Signature": "invalid_forged_signature_hex_123456",
                },
            )
            assert resp_invalid.status_code == 401
            assert "Invalid webhook signature" in resp_invalid.json()["detail"]
    finally:
        app.dependency_overrides.clear()
