import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import async_session_maker
from app.core.enums import StateTrigger, TransactionState
from app.gateways.base import GatewayStatusQueryRequest
from app.gateways.router import GatewayRouter, gateway_router
from app.models.transaction import Transactions
from app.services.state_machine import TransactionStateMachine

logger = logging.getLogger("payment_layer.reconciliation")


@dataclass
class ReconciledTransactionResult:
    transaction_id: str
    original_state: str
    resolved_state: str
    gateway_id: str
    gateway_status: str
    overridden: bool
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconciliationReport:
    scanned_count: int
    stuck_count: int
    overridden_count: int
    cutoff_minutes: int
    started_at: datetime
    completed_at: datetime
    items: List[ReconciledTransactionResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scanned_count": self.scanned_count,
            "stuck_count": self.stuck_count,
            "overridden_count": self.overridden_count,
            "cutoff_minutes": self.cutoff_minutes,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "items": [
                {
                    "transaction_id": item.transaction_id,
                    "original_state": item.original_state,
                    "resolved_state": item.resolved_state,
                    "gateway_id": item.gateway_id,
                    "gateway_status": item.gateway_status,
                    "overridden": item.overridden,
                    "details": item.details,
                }
                for item in self.items
            ],
        }


class ReconciliationEngine:
    """
    Automated Background Reconciliation Engine.
    Runs on a 15-minute schedule (or on-demand):
    1. Discovers transactions stuck in AUTH_INITIATED or CAPTURE_INITIATED for > 5 minutes.
    2. Polls the upstream payment gateway adapter for true source-of-truth status.
    3. Resolves status divergence and generates an immutable RECONCILIATION_OVERRIDE audit event.
    """

    CRON_INTERVAL_SECONDS: int = 15 * 60  # Every 15 minutes
    DEFAULT_CUTOFF_MINUTES: int = 5       # Stuck > 5 minutes

    def __init__(
        self,
        router: Optional[GatewayRouter] = None,
        session_maker: Optional[async_sessionmaker[AsyncSession]] = None,
    ):
        self.router = router or gateway_router
        self._session_maker = session_maker or async_session_maker
        self.state_machine = TransactionStateMachine(session_maker=self._session_maker)
        self._is_running = False
        self._task: Optional[asyncio.Task] = None

    async def reconcile_stuck_transactions(
        self,
        db: Optional[AsyncSession] = None,
        cutoff_minutes: int = DEFAULT_CUTOFF_MINUTES,
    ) -> ReconciliationReport:
        """
        Executes a single reconciliation sweep.
        Finds transactions stuck in AUTH_INITIATED or CAPTURE_INITIATED for > cutoff_minutes,
        queries the respective gateway, and resolves mismatches via RECONCILIATION_OVERRIDE.
        """
        start_time = datetime.now(timezone.utc)
        cutoff_timestamp = start_time - timedelta(minutes=cutoff_minutes)

        if db is not None:
            return await self._execute_reconciliation(db, cutoff_timestamp, cutoff_minutes, start_time)
        else:
            async with self._session_maker() as session:
                return await self._execute_reconciliation(session, cutoff_timestamp, cutoff_minutes, start_time)

    async def _execute_reconciliation(
        self,
        session: AsyncSession,
        cutoff_timestamp: datetime,
        cutoff_minutes: int,
        start_time: datetime,
    ) -> ReconciliationReport:
        # 1. Query transactions stuck in AUTH_INITIATED or CAPTURE_INITIATED older than cutoff
        stuck_states = [
            TransactionState.AUTH_INITIATED.value,
            TransactionState.CAPTURE_INITIATED.value,
        ]

        stmt = (
            select(Transactions)
            .where(
                Transactions.status.in_(stuck_states),
                Transactions.created_at <= cutoff_timestamp,
            )
            .order_by(Transactions.created_at.asc())
        )
        res = await session.execute(stmt)
        stuck_transactions = list(res.scalars().all())

        items: List[ReconciledTransactionResult] = []
        overridden_count = 0

        for txn in stuck_transactions:
            txn_id = txn.id
            curr_state = txn.status
            gateway_id = (txn.gateway_id or "stripe").lower().strip()
            gw = self.router.get_gateway(gateway_id)

            if not gw:
                logger.error(
                    "Reconciliation: Gateway '%s' not registered for stuck transaction %s",
                    gateway_id,
                    txn_id,
                )
                continue

            # 2. Poll the upstream gateway for true status
            gw_txn_id = txn.gateway_transaction_id or str(txn_id)
            query_req = GatewayStatusQueryRequest(
                transaction_id=txn_id,
                gateway_transaction_id=gw_txn_id,
            )

            try:
                status_resp = await gw.query_status(query_req)
                true_gw_status = status_resp.status.upper()
            except Exception as exc:
                logger.error(
                    "Reconciliation: Failed polling gateway '%s' for transaction %s: %s",
                    gateway_id,
                    txn_id,
                    str(exc),
                )
                continue

            target_state: Optional[TransactionState] = None

            # 3. Determine if there is a status mismatch
            if curr_state == TransactionState.AUTH_INITIATED.value:
                if true_gw_status in ("AUTHORIZED", "AUTHORISED"):
                    target_state = TransactionState.AUTHORISED
                elif true_gw_status == "CAPTURED":
                    target_state = TransactionState.CAPTURED
                elif true_gw_status in ("AUTH_FAILED", "FAILED", "DECLINED"):
                    target_state = TransactionState.AUTH_FAILED

            elif curr_state == TransactionState.CAPTURE_INITIATED.value:
                if true_gw_status == "CAPTURED":
                    target_state = TransactionState.CAPTURED
                elif true_gw_status in ("CAPTURE_FAILED", "FAILED"):
                    target_state = TransactionState.CAPTURE_FAILED
                elif true_gw_status == "PARTIALLY_CAPTURED":
                    target_state = TransactionState.PARTIALLY_CAPTURED

            # 4. Generate RECONCILIATION_OVERRIDE state log and transition if mismatch found
            if target_state is not None:
                logger.warning(
                    "[RECONCILIATION_MISMATCH] Transaction %s stuck in '%s', gateway '%s' reports '%s'. Overriding to '%s'.",
                    txn_id,
                    curr_state,
                    gateway_id,
                    true_gw_status,
                    target_state.value,
                )

                transition_meta = {
                    "reconciliation_reason": "Automated status reconciliation divergence detected",
                    "previous_stuck_state": curr_state,
                    "polled_gateway": gateway_id,
                    "upstream_gateway_status": true_gw_status,
                    "reconciliation_timestamp": datetime.now(timezone.utc).isoformat(),
                }

                try:
                    if curr_state == TransactionState.AUTH_INITIATED.value and target_state == TransactionState.CAPTURED:
                        # Advance through state machine graph: AUTH_INITIATED -> AUTHORISED -> CAPTURE_INITIATED -> CAPTURED
                        await self.state_machine.transition(
                            transaction_id=txn_id,
                            target_state=TransactionState.AUTHORISED,
                            trigger=StateTrigger.RECONCILIATION_OVERRIDE,
                            actor_id="system_reconciliation_engine",
                            session=session,
                        )
                        await self.state_machine.transition(
                            transaction_id=txn_id,
                            target_state=TransactionState.CAPTURE_INITIATED,
                            trigger=StateTrigger.RECONCILIATION_OVERRIDE,
                            actor_id="system_reconciliation_engine",
                            session=session,
                        )
                        await self.state_machine.transition(
                            transaction_id=txn_id,
                            target_state=TransactionState.CAPTURED,
                            trigger=StateTrigger.RECONCILIATION_OVERRIDE,
                            actor_id="system_reconciliation_engine",
                            gateway_payload=status_resp.raw_payload,
                            transition_metadata=transition_meta,
                            gateway_id=gateway_id,
                            gateway_transaction_id=status_resp.gateway_transaction_id,
                            session=session,
                        )
                    else:
                        await self.state_machine.transition(
                            transaction_id=txn_id,
                            target_state=target_state,
                            trigger=StateTrigger.RECONCILIATION_OVERRIDE,
                            actor_id="system_reconciliation_engine",
                            gateway_payload=status_resp.raw_payload,
                            transition_metadata=transition_meta,
                            gateway_id=gateway_id,
                            gateway_transaction_id=status_resp.gateway_transaction_id,
                            session=session,
                        )
                    await session.commit()

                    items.append(
                        ReconciledTransactionResult(
                            transaction_id=str(txn_id),
                            original_state=curr_state,
                            resolved_state=target_state.value,
                            gateway_id=gateway_id,
                            gateway_status=true_gw_status,
                            overridden=True,
                            details=transition_meta,
                        )
                    )
                    overridden_count += 1

                except Exception as exc:
                    logger.error(
                        "Failed applying RECONCILIATION_OVERRIDE on transaction %s: %s",
                        txn_id,
                        str(exc),
                        exc_info=True,
                    )
                    await session.rollback()
            else:
                # Gateway still reports pending / in-flight; no override needed
                items.append(
                    ReconciledTransactionResult(
                        transaction_id=str(txn_id),
                        original_state=curr_state,
                        resolved_state=curr_state,
                        gateway_id=gateway_id,
                        gateway_status=true_gw_status,
                        overridden=False,
                        details={"status": "Gateway still in-flight / no mismatch"},
                    )
                )

        end_time = datetime.now(timezone.utc)
        return ReconciliationReport(
            scanned_count=len(stuck_transactions),
            stuck_count=len(stuck_transactions),
            overridden_count=overridden_count,
            cutoff_minutes=cutoff_minutes,
            started_at=start_time,
            completed_at=end_time,
            items=items,
        )

    # -------------------------------------------------------------------------
    # BACKGROUND CRON WORKER LIFECYCLE
    # -------------------------------------------------------------------------

    async def _cron_loop(self) -> None:
        """Periodic background worker executing every 15 minutes."""
        logger.info("Reconciliation Engine cron worker started (15-min interval).")
        while self._is_running:
            try:
                await asyncio.sleep(self.CRON_INTERVAL_SECONDS)
                if not self._is_running:
                    break
                logger.info("Executing periodic 15-minute reconciliation cycle...")
                report = await self.reconcile_stuck_transactions()
                logger.info(
                    "Reconciliation cycle complete: %d scanned, %d overridden.",
                    report.scanned_count,
                    report.overridden_count,
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error during scheduled reconciliation cycle: %s", exc, exc_info=True)

    def start_cron(self) -> None:
        """Starts the standing background cron job."""
        if not self._is_running:
            self._is_running = True
            self._task = asyncio.create_task(self._cron_loop())

    def stop_cron(self) -> None:
        """Stops the standing background cron job."""
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()


# Global singleton instance
reconciliation_engine = ReconciliationEngine()
