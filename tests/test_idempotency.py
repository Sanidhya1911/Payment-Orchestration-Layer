import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.enums import IdempotencyStatus
from app.models.idempotency import IdempotencyKeys
from app.services.idempotency import (
    IdempotencyConflictException,
    IdempotencyPayloadMismatchException,
    IdempotencyService,
    InvalidIdempotencyKeyException,
)


def test_uuid_idempotency_key_validation():
    """
    MANDATE: Accepts a client-generated UUID header (Idempotency-Key).
    Rejects invalid non-UUID keys with HTTP 400.
    """
    # 1. Valid UUIDv4 must pass
    valid_uuid = str(uuid.uuid4())
    validated = IdempotencyService.validate_key(valid_uuid)
    assert validated == valid_uuid

    # 2. Invalid non-UUID strings must raise InvalidIdempotencyKeyException (HTTP 400)
    invalid_keys = [
        "not-a-uuid",
        "12345",
        "custom_key_abc",
        "xxxx-xxxx-xxxx",
        "",
        None,
    ]
    for bad_key in invalid_keys:
        with pytest.raises(InvalidIdempotencyKeyException) as exc_info:
            IdempotencyService.validate_key(bad_key)
        assert exc_info.value.status_code == 400
        assert "valid client-generated UUID" in exc_info.value.detail


def test_pg_advisory_xact_lock_id_computation():
    """
    MANDATE: Use PostgreSQL advisory locks (pg_advisory_xact_lock) on a hash
    of the idempotency key.
    Verifies that the computed hash is strictly a 64-bit signed integer (bigint).
    """
    key = str(uuid.uuid4())
    merchant_id = "merchant_prod_8829"

    lock_id = IdempotencyService.compute_advisory_lock_id(key, merchant_id)

    assert isinstance(lock_id, int)
    # Signed 64-bit bigint range: [-2^63, 2^63 - 1]
    assert -9223372036854775808 <= lock_id <= 9223372036854775807

    # Distinct keys must produce distinct lock IDs
    other_key = str(uuid.uuid4())
    other_lock_id = IdempotencyService.compute_advisory_lock_id(other_key, merchant_id)
    assert lock_id != other_lock_id


@pytest.mark.anyio
async def test_processing_status_returns_409_conflict(test_session_maker):
    """
    MANDATE: If a request has status 'PROCESSING', return a 409 Conflict.
    """
    service = IdempotencyService(session_maker=test_session_maker)
    key = str(uuid.uuid4())
    merchant_id = "merch_001"
    path = "/api/v1/transactions"
    payload = {"amount": 50000, "currency": "INR"}

    # 1. First request arrives -> status becomes 'PROCESSING'
    async with test_session_maker() as session, session.begin():
        res1 = await service.process_or_acquire(
            session=session,
            key=key,
            merchant_id=merchant_id,
            request_path=path,
            payload=payload,
        )
        assert res1.is_cached is False
        assert res1.key_record.status == IdempotencyStatus.PROCESSING.value

    # 2. Second concurrent request arrives with exact same key -> MUST raise 409 Conflict
    async with test_session_maker() as session, session.begin():
        with pytest.raises(IdempotencyConflictException) as exc_info:
            await service.process_or_acquire(
                session=session,
                key=key,
                merchant_id=merchant_id,
                request_path=path,
                payload=payload,
            )

        err = exc_info.value
        assert err.status_code == 409
        assert "PROCESSING" in err.detail
        assert err.headers.get("Retry-After") == "2"


@pytest.mark.anyio
async def test_completed_status_returns_cached_json_response(test_session_maker):
    """
    MANDATE: If 'COMPLETED', return the cached JSON response.
    """
    service = IdempotencyService(session_maker=test_session_maker)
    key = str(uuid.uuid4())
    merchant_id = "merch_001"
    path = "/api/v1/transactions"
    payload = {"amount": 75000, "currency": "INR", "order_id": "ord_999"}
    expected_response_json = {
        "id": "7bf31bfb-891f-4efc-8d14-3a7894a47683",
        "status": "CREATED",
        "amount": 75000,
        "currency": "INR",
    }

    # 1. Initial request -> PROCESSING
    async with test_session_maker() as session, session.begin():
        await service.process_or_acquire(
            session=session,
            key=key,
            merchant_id=merchant_id,
            request_path=path,
            payload=payload,
        )
        # Transaction finishes successfully -> transition to COMPLETED
        await service.save_response(
            session=session,
            key=key,
            merchant_id=merchant_id,
            response_code=201,
            response_body=expected_response_json,
            response_headers={"X-Trace-Id": "trace-1234"},
        )

    # 2. Replay request -> Returns cached JSON response without reprocessing!
    async with test_session_maker() as session, session.begin():
        res = await service.process_or_acquire(
            session=session,
            key=key,
            merchant_id=merchant_id,
            request_path=path,
            payload=payload,
        )
        assert res.is_cached is True
        assert res.response_code == 201
        assert res.response_body == expected_response_json
        assert res.response_headers == {"X-Trace-Id": "trace-1234"}


@pytest.mark.anyio
async def test_reused_key_with_altered_payload_raises_422(test_session_maker):
    """
    FINANCIAL INTEGRITY:
    Detects payload tampering if a client reuses an idempotency key with different data.
    """
    service = IdempotencyService(session_maker=test_session_maker)
    key = str(uuid.uuid4())
    merchant_id = "merch_001"
    path = "/api/v1/transactions"

    # 1. First request for ₹500 (50000 paise)
    async with test_session_maker() as session, session.begin():
        await service.process_or_acquire(
            session=session,
            key=key,
            merchant_id=merchant_id,
            request_path=path,
            payload={"amount": 50000, "currency": "INR"},
        )

    # 2. Second request with SAME key but altered amount ₹1000 (100000 paise)
    async with test_session_maker() as session, session.begin():
        with pytest.raises(IdempotencyPayloadMismatchException) as exc_info:
            await service.process_or_acquire(
                session=session,
                key=key,
                merchant_id=merchant_id,
                request_path=path,
                payload={"amount": 100000, "currency": "INR"},  # Tampered payload!
            )
        assert exc_info.value.status_code == 422
        assert "different request payload" in exc_info.value.detail


@pytest.mark.anyio
async def test_expire_and_cleanup_idempotency_keys_after_24_hours(test_session_maker):
    """
    MANDATE: Expire and clean up idempotency keys after 24 hours.
    """
    service = IdempotencyService(session_maker=test_session_maker)
    now = datetime.now(UTC)

    expired_key = str(uuid.uuid4())
    active_key = str(uuid.uuid4())
    merchant_id = "merch_ttl_test"

    async with test_session_maker() as session, session.begin():
        # 1. Insert an expired record (> 24 hours ago)
        expired_record = IdempotencyKeys(
            key=expired_key,
            merchant_id=merchant_id,
            request_path="/api/v1/transactions",
            request_hash="hash_expired",
            status=IdempotencyStatus.COMPLETED.value,
            response_code=200,
            response_body={"msg": "old"},
            expires_at=now - timedelta(hours=1),  # Expired
        )
        # 2. Insert an active record (< 24 hours)
        active_record = IdempotencyKeys(
            key=active_key,
            merchant_id=merchant_id,
            request_path="/api/v1/transactions",
            request_hash="hash_active",
            status=IdempotencyStatus.COMPLETED.value,
            response_code=200,
            response_body={"msg": "fresh"},
            expires_at=now + timedelta(hours=23),  # Active
        )
        session.add_all([expired_record, active_record])

    # 3. Trigger cleanup
    async with test_session_maker() as session, session.begin():
        pruned_count = await service.cleanup_expired_keys(session=session)
        assert pruned_count == 1

    # 4. Verify expired record is deleted, active record is preserved
    async with test_session_maker() as session:
        res = await session.execute(
            select(IdempotencyKeys).where(IdempotencyKeys.merchant_id == merchant_id)
        )
        remaining = res.scalars().all()
        assert len(remaining) == 1
        assert remaining[0].key == active_key


@pytest.mark.anyio
async def test_idempotent_scope_context_manager(test_session_maker):
    """Verify async context manager behavior for endpoint integration."""
    service = IdempotencyService(session_maker=test_session_maker)
    key = str(uuid.uuid4())
    merchant_id = "merch_scope"
    payload = {"amount": 25000, "currency": "INR"}

    # Run 1: Execution proceeds and caches response
    async with test_session_maker() as session, session.begin():
        async with service.idempotent_scope(
            session=session,
            key=key,
            merchant_id=merchant_id,
            request_path="/api/v1/transactions",
            payload=payload,
        ) as idemp:
            assert idemp.is_cached is False
            # Simulate work
            computed_data = {"transaction_id": "txn_8829", "status": "CREATED"}
            idemp.set_response(status_code=201, body=computed_data)

    # Run 2: Execution is bypassed, cached response is yielded immediately
    async with test_session_maker() as session, session.begin():
        async with service.idempotent_scope(
            session=session,
            key=key,
            merchant_id=merchant_id,
            request_path="/api/v1/transactions",
            payload=payload,
        ) as idemp:
            assert idemp.is_cached is True
            assert idemp.cached_code == 201
            assert idemp.cached_response == {
                "transaction_id": "txn_8829",
                "status": "CREATED",
            }
