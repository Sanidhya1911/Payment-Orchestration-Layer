import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.enums import StateTrigger, TransactionState
from app.models import Base, Transactions, TransactionStateLog
from app.services.state_machine import (
    InvalidStateTransitionException,
    TransactionStateMachine,
)


@pytest.fixture
async def test_db_session_maker():
    """Provides an isolated in-memory SQLite database sessionmaker for async testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    yield session_maker
    await engine.dispose()


@pytest.fixture
async def sample_transaction(test_db_session_maker) -> Transactions:
    """Creates a baseline transaction in CREATED state."""
    async with test_db_session_maker() as session, session.begin():
        txn = Transactions(
            id=uuid.uuid4(),
            merchant_id="merchant_test_01",
            order_id="order_12345",
            amount=50000,  # 500.00 INR (strictly BIGINT paise)
            currency="INR",
            status=TransactionState.CREATED.value,
            payment_method="UPI",
        )
        session.add(txn)
        await session.flush()
        return txn


def test_twelve_strict_states_defined():
    """
    MANDATE:
    Verify the 12 strict states are precisely defined:
    CREATED, ROUTE_SELECTED, AUTH_INITIATED, AUTHORISED, AUTH_FAILED,
    CAPTURE_INITIATED, CAPTURED, PARTIALLY_CAPTURED, CAPTURE_FAILED,
    REFUND_INITIATED, REFUNDED, FAILED.
    """
    expected_states = {
        "CREATED",
        "ROUTE_SELECTED",
        "AUTH_INITIATED",
        "AUTHORISED",
        "AUTH_FAILED",
        "CAPTURE_INITIATED",
        "CAPTURED",
        "PARTIALLY_CAPTURED",
        "CAPTURE_FAILED",
        "REFUND_INITIATED",
        "REFUNDED",
        "FAILED",
    }
    actual_states = {s.value for s in TransactionState}
    assert (
        expected_states == actual_states
    ), f"Mismatch in 12 states. Expected {expected_states}, got {actual_states}"


def test_invalid_state_transition_throws_exception():
    """
    MANDATE:
    Write transition logic that throws an InvalidStateTransitionException
    if an illegal transition is attempted (e.g., CREATED directly to REFUNDED).
    """
    # 1. Test specifically cited illegal transition: CREATED -> REFUNDED
    with pytest.raises(InvalidStateTransitionException) as exc_info:
        TransactionStateMachine.validate_transition(
            current_state=TransactionState.CREATED,
            target_state=TransactionState.REFUNDED,
            transaction_id="txn_test_123",
            trigger="TEST_TRIGGER",
        )

    err = exc_info.value
    assert err.current_state == "CREATED"
    assert err.target_state == "REFUNDED"
    assert "Invalid state transition from 'CREATED' to 'REFUNDED'" in str(err)
    assert "ROUTE_SELECTED" in str(err)

    # 2. Test other illegal transitions:
    # CREATED -> CAPTURED
    with pytest.raises(InvalidStateTransitionException):
        TransactionStateMachine.validate_transition(
            TransactionState.CREATED,
            TransactionState.CAPTURED,
        )

    # CREATED -> AUTH_INITIATED (must select route first)
    with pytest.raises(InvalidStateTransitionException):
        TransactionStateMachine.validate_transition(
            TransactionState.CREATED,
            TransactionState.AUTH_INITIATED,
        )

    # AUTH_INITIATED -> CAPTURED (must be AUTHORISED first)
    with pytest.raises(InvalidStateTransitionException):
        TransactionStateMachine.validate_transition(
            TransactionState.AUTH_INITIATED,
            TransactionState.CAPTURED,
        )

    # Terminal state transitions must be rejected
    with pytest.raises(InvalidStateTransitionException):
        TransactionStateMachine.validate_transition(
            TransactionState.REFUNDED,
            TransactionState.CREATED,
        )

    with pytest.raises(InvalidStateTransitionException):
        TransactionStateMachine.validate_transition(
            TransactionState.FAILED,
            TransactionState.AUTHORISED,
        )


def test_valid_deterministic_lifecycle():
    """Verify standard happy-path transition paths validate successfully."""
    # Standard Flow: CREATED -> ROUTE_SELECTED -> AUTH_INITIATED -> AUTHORISED -> CAPTURE_INITIATED -> CAPTURED -> REFUND_INITIATED -> REFUNDED
    path = [
        (TransactionState.CREATED, TransactionState.ROUTE_SELECTED),
        (TransactionState.ROUTE_SELECTED, TransactionState.AUTH_INITIATED),
        (TransactionState.AUTH_INITIATED, TransactionState.AUTHORISED),
        (TransactionState.AUTHORISED, TransactionState.CAPTURE_INITIATED),
        (TransactionState.CAPTURE_INITIATED, TransactionState.CAPTURED),
        (TransactionState.CAPTURED, TransactionState.REFUND_INITIATED),
        (TransactionState.REFUND_INITIATED, TransactionState.REFUNDED),
    ]
    for curr, tgt in path:
        # Should not raise
        TransactionStateMachine.validate_transition(curr, tgt)


@pytest.mark.anyio
async def test_transition_writes_state_log_automatically(test_db_session_maker, sample_transaction):
    """
    MANDATE:
    Every state transition MUST automatically write a log entry to the
    transaction_state_log table with the previous state, new state, and trigger event.
    """
    sm = TransactionStateMachine(session_maker=test_db_session_maker)

    # Transition 1: CREATED -> ROUTE_SELECTED
    updated_txn = await sm.transition(
        transaction_id=sample_transaction.id,
        target_state=TransactionState.ROUTE_SELECTED,
        trigger=StateTrigger.ROUTE_SELECTED,
        actor_id="service:smart_router",
        transition_metadata={"gateway_chosen": "stripe_us"},
    )
    assert updated_txn.status == TransactionState.ROUTE_SELECTED.value

    # Verify state log was inserted
    async with test_db_session_maker() as session:
        logs_res = await session.execute(
            select(TransactionStateLog)
            .where(TransactionStateLog.transaction_id == sample_transaction.id)
            .order_by(TransactionStateLog.id)
        )
        logs = logs_res.scalars().all()

        assert len(logs) == 1
        log = logs[0]
        assert log.from_state == TransactionState.CREATED.value
        assert log.to_state == TransactionState.ROUTE_SELECTED.value
        assert log.trigger == StateTrigger.ROUTE_SELECTED.value
        assert log.actor_id == "service:smart_router"
        assert log.transition_metadata == {"gateway_chosen": "stripe_us"}

    # Transition 2: ROUTE_SELECTED -> AUTH_INITIATED
    await sm.transition(
        transaction_id=sample_transaction.id,
        target_state=TransactionState.AUTH_INITIATED,
        trigger=StateTrigger.GATEWAY_CALL,
        actor_id="service:payment_core",
    )

    async with test_db_session_maker() as session:
        logs_res = await session.execute(
            select(TransactionStateLog)
            .where(TransactionStateLog.transaction_id == sample_transaction.id)
            .order_by(TransactionStateLog.id)
        )
        logs = logs_res.scalars().all()

        assert len(logs) == 2
        assert logs[1].from_state == TransactionState.ROUTE_SELECTED.value
        assert logs[1].to_state == TransactionState.AUTH_INITIATED.value
        assert logs[1].trigger == StateTrigger.GATEWAY_CALL.value


@pytest.mark.anyio
async def test_gateway_call_releases_lock_before_external_api(
    test_db_session_maker, sample_transaction
):
    """
    MANDATE:
    Use pessimistic locking (SELECT FOR UPDATE) during the transition process,
    but ensure the lock is released before making external gateway API calls.
    """
    sm = TransactionStateMachine(session_maker=test_db_session_maker)

    # Move to ROUTE_SELECTED first
    await sm.transition(
        transaction_id=sample_transaction.id,
        target_state=TransactionState.ROUTE_SELECTED,
        trigger=StateTrigger.ROUTE_SELECTED,
        actor_id="service:router",
    )

    gateway_call_executed = False
    lock_released_during_call = False

    async def mock_external_gateway_api():
        nonlocal gateway_call_executed, lock_released_during_call
        gateway_call_executed = True

        # Prove lock is released: While inside this gateway call,
        # open another concurrent session and verify the transaction is visible
        # and has status AUTH_INITIATED committed in the DB!
        async with test_db_session_maker() as concurrent_session:
            stmt = select(Transactions).where(Transactions.id == sample_transaction.id)
            res = await concurrent_session.execute(stmt)
            txn = res.scalar_one()

            # The in-flight status MUST already be committed and visible,
            # meaning Phase 1 committed and released its lock!
            assert txn.status == TransactionState.AUTH_INITIATED.value
            lock_released_during_call = True

        # Simulate network latency
        await asyncio.sleep(0.01)

        return {
            "charge_id": "ch_stripe_992819",
            "arn": "ARN_1234567890",
            "status": "succeeded",
        }

    # Execute gateway operation with lock released during external call
    result = await sm.execute_gateway_call(
        transaction_id=sample_transaction.id,
        in_flight_state=TransactionState.AUTH_INITIATED,
        trigger=StateTrigger.GATEWAY_CALL,
        actor_id="worker:payment_processor",
        gateway_call=mock_external_gateway_api,
        on_success_state=TransactionState.AUTHORISED,
        on_failure_state=TransactionState.AUTH_FAILED,
        gateway_id="stripe_us",
        extract_success_details=lambda resp: (resp["charge_id"], resp["arn"], resp),
    )

    assert result.success is True
    assert gateway_call_executed is True
    assert lock_released_during_call is True
    assert result.transaction.status == TransactionState.AUTHORISED.value
    assert result.transaction.gateway_transaction_id == "ch_stripe_992819"
    assert result.transaction.gateway_reference_id == "ARN_1234567890"

    # Verify both transitions were logged automatically
    async with test_db_session_maker() as session:
        logs_res = await session.execute(
            select(TransactionStateLog)
            .where(TransactionStateLog.transaction_id == sample_transaction.id)
            .order_by(TransactionStateLog.id)
        )
        logs = logs_res.scalars().all()

        # Log 1: CREATED -> ROUTE_SELECTED
        # Log 2: ROUTE_SELECTED -> AUTH_INITIATED (Pre-call lock phase)
        # Log 3: AUTH_INITIATED -> AUTHORISED    (Post-call lock phase)
        assert len(logs) == 3
        assert logs[1].from_state == TransactionState.ROUTE_SELECTED.value
        assert logs[1].to_state == TransactionState.AUTH_INITIATED.value
        assert logs[2].from_state == TransactionState.AUTH_INITIATED.value
        assert logs[2].to_state == TransactionState.AUTHORISED.value
        assert logs[2].gateway_payload == {
            "charge_id": "ch_stripe_992819",
            "arn": "ARN_1234567890",
            "status": "succeeded",
        }


@pytest.mark.anyio
async def test_gateway_call_failure_records_failed_state_and_log(
    test_db_session_maker, sample_transaction
):
    """Verify that if an external gateway API fails/times out, lock re-acquisition transitions to failure."""
    sm = TransactionStateMachine(session_maker=test_db_session_maker)

    await sm.transition(
        transaction_id=sample_transaction.id,
        target_state=TransactionState.ROUTE_SELECTED,
        trigger=StateTrigger.ROUTE_SELECTED,
        actor_id="service:router",
    )

    async def mock_failing_gateway_api():
        await asyncio.sleep(0.01)
        raise ConnectionResetError("Gateway TCP connection dropped")

    result = await sm.execute_gateway_call(
        transaction_id=sample_transaction.id,
        in_flight_state=TransactionState.AUTH_INITIATED,
        trigger=StateTrigger.GATEWAY_CALL,
        actor_id="worker:payment_processor",
        gateway_call=mock_failing_gateway_api,
        on_success_state=TransactionState.AUTHORISED,
        on_failure_state=TransactionState.AUTH_FAILED,
        gateway_id="stripe_us",
    )

    assert result.success is False
    assert isinstance(result.error, ConnectionResetError)
    assert (
        result.transaction.status == TransactionState.FAILED.value
        or result.transaction.status == TransactionState.AUTH_FAILED.value
    )
    assert "Gateway TCP connection dropped" in result.transaction.error_message


@pytest.mark.anyio
async def test_gateway_transition_scope_context_manager(test_db_session_maker, sample_transaction):
    """Test the async context manager pattern for gateway transition isolation."""
    sm = TransactionStateMachine(session_maker=test_db_session_maker)

    # Transition to ROUTE_SELECTED
    await sm.transition(
        sample_transaction.id,
        TransactionState.ROUTE_SELECTED,
        StateTrigger.ROUTE_SELECTED,
        "service:router",
    )

    async with sm.gateway_transition_scope(
        transaction_id=sample_transaction.id,
        in_flight_state=TransactionState.AUTH_INITIATED,
        trigger=StateTrigger.GATEWAY_CALL,
        actor_id="service:checkout",
        gateway_id="razorpay_in",
    ) as scope:
        # Zero locks held during this block!
        fake_api_response = {"id": "pay_982181", "fee": 150}
        scope.set_success(
            target_state=TransactionState.AUTHORISED,
            gateway_payload=fake_api_response,
            gateway_transaction_id="pay_982181",
        )

    # Verify transaction transitioned to AUTHORISED
    async with test_db_session_maker() as session:
        res = await session.execute(
            select(Transactions).where(Transactions.id == sample_transaction.id)
        )
        txn = res.scalar_one()
        assert txn.status == TransactionState.AUTHORISED.value
        assert txn.gateway_transaction_id == "pay_982181"
