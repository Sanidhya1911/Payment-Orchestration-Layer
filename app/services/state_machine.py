import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import (
    Any,
    Generic,
    TypeVar,
)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import async_session_maker
from app.core.enums import StateTrigger, TransactionState
from app.models.transaction import Transactions, TransactionStateLog

logger = logging.getLogger("payment_layer.state_machine")

T = TypeVar("T")


class InvalidStateTransitionException(Exception):
    """
    Raised when an illegal transaction state transition is attempted.
    Adheres to strict deterministic finite-state machine enforcement.
    """

    def __init__(
        self,
        current_state: str | TransactionState,
        target_state: str | TransactionState,
        transaction_id: str | uuid.UUID | None = None,
        trigger: str | None = None,
        allowed_transitions: set[str | TransactionState] | None = None,
    ):
        self.current_state = (
            current_state.value
            if isinstance(current_state, TransactionState)
            else str(current_state)
        )
        self.target_state = (
            target_state.value if isinstance(target_state, TransactionState) else str(target_state)
        )
        self.transaction_id = str(transaction_id) if transaction_id else None
        self.trigger = str(trigger) if trigger else None
        self.allowed_transitions = {
            s.value if isinstance(s, TransactionState) else str(s)
            for s in (allowed_transitions or set())
        }

        msg = (
            f"Invalid state transition from '{self.current_state}' to '{self.target_state}' "
            f"for transaction '{self.transaction_id or 'UNKNOWN'}' "
            f"(trigger: '{self.trigger or 'UNSPECIFIED'}'). "
            f"Allowed transitions from '{self.current_state}': "
            f"{sorted(list(self.allowed_transitions)) or 'None (terminal state)'}."
        )
        super().__init__(msg)


class TransactionNotFoundException(Exception):
    """Raised when a transaction record cannot be found in the ledger."""

    def __init__(self, transaction_id: str | uuid.UUID):
        self.transaction_id = str(transaction_id)
        super().__init__(f"Transaction '{self.transaction_id}' was not found in the database.")


class ConcurrentStateMutationException(Exception):
    """
    Raised if transaction state changed unexpectedly while lock was released during
    external gateway execution (e.g. concurrent webhook callback or sweeper).
    """

    def __init__(self, transaction_id: str | uuid.UUID, expected_state: str, actual_state: str):
        self.transaction_id = str(transaction_id)
        self.expected_state = expected_state
        self.actual_state = actual_state
        super().__init__(
            f"Transaction '{self.transaction_id}' state was concurrently mutated "
            f"from '{expected_state}' to '{actual_state}' while gateway call was in flight."
        )


# =============================================================================
# DETERMINISTIC STATE MACHINE TRANSITION GRAPH (12 STRICT STATES)
# =============================================================================
ALLOWED_TRANSITIONS: dict[TransactionState, set[TransactionState]] = {
    # 1. CREATED
    TransactionState.CREATED: {
        TransactionState.ROUTE_SELECTED,
        TransactionState.FAILED,
    },
    # 2. ROUTE_SELECTED
    TransactionState.ROUTE_SELECTED: {
        TransactionState.AUTH_INITIATED,
        TransactionState.FAILED,
    },
    # 3. AUTH_INITIATED
    TransactionState.AUTH_INITIATED: {
        TransactionState.AUTHORISED,
        TransactionState.AUTH_FAILED,
        TransactionState.FAILED,
    },
    # 4. AUTHORISED
    TransactionState.AUTHORISED: {
        TransactionState.CAPTURE_INITIATED,
        TransactionState.REFUND_INITIATED,  # Auth reversal / void
        TransactionState.FAILED,  # Auth expiration / cancellation
    },
    # 5. AUTH_FAILED
    TransactionState.AUTH_FAILED: {
        TransactionState.ROUTE_SELECTED,  # Smart retry / failover route
        TransactionState.FAILED,  # Terminal failure
    },
    # 6. CAPTURE_INITIATED
    TransactionState.CAPTURE_INITIATED: {
        TransactionState.CAPTURED,
        TransactionState.PARTIALLY_CAPTURED,
        TransactionState.CAPTURE_FAILED,
        TransactionState.FAILED,
    },
    # 7. CAPTURED
    TransactionState.CAPTURED: {
        TransactionState.REFUND_INITIATED,
    },
    # 8. PARTIALLY_CAPTURED
    TransactionState.PARTIALLY_CAPTURED: {
        TransactionState.CAPTURE_INITIATED,  # Incremental capture for remaining balance
        TransactionState.REFUND_INITIATED,  # Refund against captured amount
    },
    # 9. CAPTURE_FAILED
    TransactionState.CAPTURE_FAILED: {
        TransactionState.CAPTURE_INITIATED,  # Retry capture before auth expires
        TransactionState.FAILED,  # Terminal capture failure
    },
    # 10. REFUND_INITIATED
    TransactionState.REFUND_INITIATED: {
        TransactionState.REFUNDED,
        TransactionState.CAPTURED,  # Refund rejected: revert to captured
        TransactionState.PARTIALLY_CAPTURED,  # Refund rejected: revert to partially captured
        TransactionState.FAILED,  # Unrecoverable refund failure
    },
    # 11. REFUNDED (Terminal)
    TransactionState.REFUNDED: set(),
    # 12. FAILED (Terminal)
    TransactionState.FAILED: set(),
}


@dataclass
class GatewayScopeResult:
    """Carries completion payload for a gateway transition scope."""

    target_state: TransactionState | None = None
    trigger: str | None = None
    gateway_payload: dict[str, Any] | None = None
    gateway_transaction_id: str | None = None
    gateway_reference_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    transition_metadata: dict[str, Any] | None = None

    def set_success(
        self,
        target_state: TransactionState,
        trigger: str | None = None,
        gateway_payload: dict[str, Any] | None = None,
        gateway_transaction_id: str | None = None,
        gateway_reference_id: str | None = None,
        transition_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.target_state = target_state
        self.trigger = trigger
        self.gateway_payload = gateway_payload
        self.gateway_transaction_id = gateway_transaction_id
        self.gateway_reference_id = gateway_reference_id
        self.transition_metadata = transition_metadata

    def set_failure(
        self,
        target_state: TransactionState,
        trigger: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        gateway_payload: dict[str, Any] | None = None,
        transition_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.target_state = target_state
        self.trigger = trigger
        self.error_code = error_code
        self.error_message = error_message
        self.gateway_payload = gateway_payload
        self.transition_metadata = transition_metadata


@dataclass
class GatewayExecutionResult(Generic[T]):
    """Result of an orchestrated gateway execution."""

    success: bool
    transaction: Transactions
    gateway_response: T | None = None
    error: Exception | None = None


class TransactionStateMachine:
    """
    Deterministic Transaction State Machine with:
    1. 12 Strict Transaction States.
    2. Pessimistic Row Locking (SELECT ... FOR UPDATE) during state changes.
    3. Mandatory Lock Release BEFORE external Gateway HTTP/API calls.
    4. Automatic, immutable audit log generation into transaction_state_log.
    """

    def __init__(self, session_maker: async_sessionmaker[AsyncSession] = async_session_maker):
        self._session_maker = session_maker

    @classmethod
    def get_allowed_transitions(cls, state: str | TransactionState) -> set[TransactionState]:
        """Returns the set of permissible target states from a given state."""
        state_enum = state if isinstance(state, TransactionState) else TransactionState(state)
        return ALLOWED_TRANSITIONS.get(state_enum, set())

    @classmethod
    def validate_transition(
        cls,
        current_state: str | TransactionState,
        target_state: str | TransactionState,
        transaction_id: str | uuid.UUID | None = None,
        trigger: str | None = None,
    ) -> None:
        """
        Validates whether current_state -> target_state is legal.
        Raises InvalidStateTransitionException if the transition is prohibited.
        """
        curr = (
            current_state
            if isinstance(current_state, TransactionState)
            else TransactionState(current_state)
        )
        tgt = (
            target_state
            if isinstance(target_state, TransactionState)
            else TransactionState(target_state)
        )

        allowed = ALLOWED_TRANSITIONS.get(curr, set())
        if tgt not in allowed:
            raise InvalidStateTransitionException(
                current_state=curr,
                target_state=tgt,
                transaction_id=transaction_id,
                trigger=trigger,
                allowed_transitions=allowed,
            )

    async def transition(
        self,
        transaction_id: uuid.UUID,
        target_state: str | TransactionState,
        trigger: str | StateTrigger,
        actor_id: str,
        gateway_payload: dict[str, Any] | None = None,
        transition_metadata: dict[str, Any] | None = None,
        gateway_id: str | None = None,
        gateway_transaction_id: str | None = None,
        gateway_reference_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        session: AsyncSession | None = None,
    ) -> Transactions:
        """
        Performs an atomic state transition under pessimistic lock (SELECT FOR UPDATE).
        Automatically inserts an immutable audit log entry in transaction_state_log,
        and commits immediately to release the lock.
        """
        target_enum = (
            target_state
            if isinstance(target_state, TransactionState)
            else TransactionState(target_state)
        )
        trigger_str = trigger.value if isinstance(trigger, StateTrigger) else str(trigger)

        if session is not None:
            # Caller provided external session
            return await self._execute_transition_step(
                session=session,
                transaction_id=transaction_id,
                target_state=target_enum,
                trigger=trigger_str,
                actor_id=actor_id,
                gateway_payload=gateway_payload,
                transition_metadata=transition_metadata,
                gateway_id=gateway_id,
                gateway_transaction_id=gateway_transaction_id,
                gateway_reference_id=gateway_reference_id,
                error_code=error_code,
                error_message=error_message,
            )
        else:
            # Self-managed atomic session with automatic commit to release row lock
            async with self._session_maker() as sess:
                async with sess.begin():
                    txn = await self._execute_transition_step(
                        session=sess,
                        transaction_id=transaction_id,
                        target_state=target_enum,
                        trigger=trigger_str,
                        actor_id=actor_id,
                        gateway_payload=gateway_payload,
                        transition_metadata=transition_metadata,
                        gateway_id=gateway_id,
                        gateway_transaction_id=gateway_transaction_id,
                        gateway_reference_id=gateway_reference_id,
                        error_code=error_code,
                        error_message=error_message,
                    )
                # Lock is officially released here upon sess.begin() context exit (COMMIT)
                return txn

    async def _execute_transition_step(
        self,
        session: AsyncSession,
        transaction_id: uuid.UUID,
        target_state: TransactionState,
        trigger: str,
        actor_id: str,
        gateway_payload: dict[str, Any] | None = None,
        transition_metadata: dict[str, Any] | None = None,
        gateway_id: str | None = None,
        gateway_transaction_id: str | None = None,
        gateway_reference_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> Transactions:
        """
        Internal implementation of the locked transition step.
        Uses SELECT FOR UPDATE on the transactions table.
        """
        # 1. Pessimistic Row Lock: SELECT ... FOR UPDATE
        stmt = select(Transactions).where(Transactions.id == transaction_id).with_for_update()
        result = await session.execute(stmt)
        txn = result.scalar_one_or_none()

        if txn is None:
            raise TransactionNotFoundException(transaction_id)

        previous_state = txn.status

        # 2. Enforce Deterministic Transition Validation
        self.validate_transition(
            current_state=previous_state,
            target_state=target_state,
            transaction_id=transaction_id,
            trigger=trigger,
        )

        # 3. Update Transaction Record
        txn.status = target_state.value
        if gateway_id is not None:
            txn.gateway_id = gateway_id
        if gateway_transaction_id is not None:
            txn.gateway_transaction_id = gateway_transaction_id
        if gateway_reference_id is not None:
            txn.gateway_reference_id = gateway_reference_id
        if error_code is not None:
            txn.error_code = error_code
        if error_message is not None:
            txn.error_message = error_message

        # 4. Mandatory State Log Insertion (Immutable Audit Trail)
        log_entry = TransactionStateLog(
            transaction_id=txn.id,
            from_state=previous_state,
            to_state=target_state.value,
            trigger=trigger,
            actor_id=actor_id or "system",
            gateway_payload=gateway_payload,
            transition_metadata=transition_metadata,
        )
        session.add(log_entry)
        await session.flush()

        logger.info(
            "State transition successful for transaction %s: %s -> %s (trigger: %s)",
            transaction_id,
            previous_state,
            target_state.value,
            trigger,
        )
        return txn

    @asynccontextmanager
    async def gateway_transition_scope(
        self,
        transaction_id: uuid.UUID,
        in_flight_state: TransactionState,
        trigger: str | StateTrigger,
        actor_id: str,
        gateway_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[GatewayScopeResult]:
        """
        Context manager enforcing the payment architect isolation pattern:
        1. Acquire pessimistic lock (SELECT FOR UPDATE).
        2. Transition to in-flight state (e.g. AUTH_INITIATED, CAPTURE_INITIATED).
        3. Write state log entry.
        4. COMMIT & RELEASE lock.
        5. Yield to caller -> Caller makes external Gateway API call with ZERO DB locks held.
        6. Re-acquire pessimistic lock (SELECT FOR UPDATE).
        7. Validate that state was not concurrently mutated during the gateway call.
        8. Transition to final target state, write state log entry.
        9. COMMIT & RELEASE lock.
        """
        trigger_str = trigger.value if isinstance(trigger, StateTrigger) else str(trigger)
        scope_result = GatewayScopeResult()

        # ---------------------------------------------------------------------
        # PHASE 1: PRE-CALL IN-FLIGHT TRANSITION (LOCK ACQUIRED -> RELEASED)
        # ---------------------------------------------------------------------
        async with self._session_maker() as sess, sess.begin():
            await self._execute_transition_step(
                session=sess,
                transaction_id=transaction_id,
                target_state=in_flight_state,
                trigger=trigger_str,
                actor_id=actor_id,
                gateway_id=gateway_id,
                transition_metadata=metadata,
            )
        # Lock is completely RELEASED here upon session commit!

        # ---------------------------------------------------------------------
        # PHASE 2: CALLER EXECUTES GATEWAY API CALL (ZERO LOCKS HELD)
        # ---------------------------------------------------------------------
        caught_exception: Exception | None = None
        try:
            yield scope_result
        except Exception as exc:
            caught_exception = exc
            logger.exception(
                "Exception occurred during external gateway call for transaction %s: %s",
                transaction_id,
                str(exc),
            )

        # ---------------------------------------------------------------------
        # PHASE 3: POST-CALL TERMINAL/NEXT TRANSITION (LOCK RE-ACQUIRED -> RELEASED)
        # ---------------------------------------------------------------------
        async with self._session_maker() as sess, sess.begin():
            # Re-acquire row lock
            stmt = select(Transactions).where(Transactions.id == transaction_id).with_for_update()
            res = await sess.execute(stmt)
            txn = res.scalar_one_or_none()
            if txn is None:
                raise TransactionNotFoundException(transaction_id)

            # Verify state wasn't concurrently tampered with while lock was released
            if txn.status != in_flight_state.value:
                logger.warning(
                    "Transaction %s state changed from %s to %s during external call",
                    transaction_id,
                    in_flight_state.value,
                    txn.status,
                )
                raise ConcurrentStateMutationException(
                    transaction_id=transaction_id,
                    expected_state=in_flight_state.value,
                    actual_state=txn.status,
                )

            # Determine post-call outcome
            if caught_exception is not None:
                final_target = scope_result.target_state or TransactionState.FAILED
                final_trigger = scope_result.trigger or StateTrigger.GATEWAY_TIMEOUT.value
                err_code = scope_result.error_code or "GATEWAY_ERROR"
                err_msg = scope_result.error_message or str(caught_exception)
            else:
                if scope_result.target_state is None:
                    raise ValueError(
                        "Gateway transition scope exited without specifying target_state via "
                        "scope.set_success(...) or scope.set_failure(...)."
                    )
                final_target = scope_result.target_state
                final_trigger = scope_result.trigger or StateTrigger.GATEWAY_RESPONSE.value
                err_code = scope_result.error_code
                err_msg = scope_result.error_message

            await self._execute_transition_step(
                session=sess,
                transaction_id=transaction_id,
                target_state=final_target,
                trigger=final_trigger,
                actor_id=actor_id,
                gateway_payload=scope_result.gateway_payload,
                transition_metadata=scope_result.transition_metadata,
                gateway_id=gateway_id,
                gateway_transaction_id=scope_result.gateway_transaction_id,
                gateway_reference_id=scope_result.gateway_reference_id,
                error_code=err_code,
                error_message=err_msg,
            )
        # Post-call lock is completely RELEASED here!

        if caught_exception is not None:
            raise caught_exception

    async def execute_gateway_call(
        self,
        transaction_id: uuid.UUID,
        in_flight_state: TransactionState,
        trigger: str | StateTrigger,
        actor_id: str,
        gateway_call: Callable[[], Awaitable[T]],
        on_success_state: TransactionState,
        on_failure_state: TransactionState,
        gateway_id: str | None = None,
        extract_success_details: (
            Callable[[T], tuple[str | None, str | None, dict[str, Any] | None]] | None
        ) = None,
        extract_failure_details: (
            Callable[[Exception], tuple[str | None, str | None, dict[str, Any] | None]] | None
        ) = None,
        metadata: dict[str, Any] | None = None,
    ) -> GatewayExecutionResult[T]:
        """
        Orchestrates a gateway call around two separate short-lived pessimistic locks.
        The external gateway call is invoked completely outside database transactions.
        """
        try:
            async with self.gateway_transition_scope(
                transaction_id=transaction_id,
                in_flight_state=in_flight_state,
                trigger=trigger,
                actor_id=actor_id,
                gateway_id=gateway_id,
                metadata=metadata,
            ) as scope:
                # Gateway call executed with ZERO locks held:
                response = await gateway_call()

                gw_txn_id, gw_ref_id, gw_payload = None, None, None
                if extract_success_details is not None:
                    gw_txn_id, gw_ref_id, gw_payload = extract_success_details(response)

                scope.set_success(
                    target_state=on_success_state,
                    trigger=StateTrigger.GATEWAY_RESPONSE.value,
                    gateway_payload=gw_payload,
                    gateway_transaction_id=gw_txn_id,
                    gateway_reference_id=gw_ref_id,
                )

            # Retrieve refreshed transaction
            async with self._session_maker() as sess:
                res = await sess.execute(
                    select(Transactions).where(Transactions.id == transaction_id)
                )
                updated_txn = res.scalar_one()

            return GatewayExecutionResult(
                success=True,
                transaction=updated_txn,
                gateway_response=response,
            )

        except Exception as exc:
            # Failure transition was recorded in gateway_transition_scope __aexit__
            async with self._session_maker() as sess:
                res = await sess.execute(
                    select(Transactions).where(Transactions.id == transaction_id)
                )
                updated_txn = res.scalar_one_or_none()

            return GatewayExecutionResult(
                success=False,
                transaction=updated_txn,
                error=exc,
            )
