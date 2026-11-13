import hashlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import StateTrigger, TransactionState, WebhookProcessingStatus
from app.models.webhook import ProcessedWebhookEvents

logger = logging.getLogger("payment_layer.webhook")


def timingSafeEqual(a: str | bytes, b: str | bytes) -> bool:
    """
    Constant-time string/bytes comparison using Python's hmac.compare_digest
    to eliminate timing side-channel attacks.
    Adheres strictly to the timingSafeEqual mandate.
    """
    if isinstance(a, str):
        a = a.encode("utf-8")
    if isinstance(b, str):
        b = b.encode("utf-8")
    return hmac.compare_digest(a, b)


# Canonical alias
timing_safe_equal = timingSafeEqual


class WebhookSecurityException(Exception):
    """Base exception for webhook security and authentication errors."""


class MissingWebhookSignatureException(WebhookSecurityException):
    """Raised when the mandatory HMAC signature header is missing."""

    def __init__(self, gateway: str, expected_header: str):
        self.gateway = gateway
        self.expected_header = expected_header
        super().__init__(
            f"Missing required webhook signature header '{expected_header}' for gateway '{gateway}'"
        )


class InvalidWebhookSignatureException(WebhookSecurityException):
    """Raised when HMAC signature verification fails (timing safe check)."""

    def __init__(self, gateway: str, reason: str = "Signature mismatch"):
        self.gateway = gateway
        self.reason = reason
        super().__init__(f"Invalid HMAC signature for gateway '{gateway}': {reason}")


class WebhookProcessingException(Exception):
    """Raised when an internal error occurs during webhook processing."""


# =============================================================================
# GATEWAY HMAC SIGNATURE HEADERS & CONFIGURATION
# =============================================================================

GATEWAY_SIGNATURE_HEADERS: dict[str, list[str]] = {
    "razorpay": ["x-razorpay-signature", "x-signature"],
    "stripe": ["stripe-signature"],
    "payu": ["x-payu-signature", "payu-signature"],
    "upi": ["x-upi-signature", "x-npci-signature", "x-signature"],
}


def get_gateway_webhook_secret(gateway: str, override_secret: str | None = None) -> str:
    """Retrieves the HMAC secret configured for the specified payment gateway."""
    if override_secret:
        return override_secret

    gw = gateway.lower().strip()
    if gw == "razorpay":
        return settings.RAZORPAY_WEBHOOK_SECRET
    elif gw == "stripe":
        return settings.STRIPE_WEBHOOK_SECRET
    elif gw == "payu":
        return settings.PAYU_WEBHOOK_SECRET
    elif gw == "upi":
        return settings.UPI_WEBHOOK_SECRET
    return f"whsec_{gw}_default_secret_key_32"


def generate_webhook_signature(
    gateway: str,
    raw_body: bytes,
    secret: str | None = None,
    timestamp: int | None = None,
) -> tuple[str, str]:
    """
    Test and integration utility: Computes a compliant HMAC-SHA256 signature
    and returns (header_name, header_value) for any of the 4 gateways.
    """
    gw = gateway.lower().strip()
    sec = get_gateway_webhook_secret(gw, secret)

    if gw == "stripe":
        ts = timestamp or int(datetime.now(UTC).timestamp())
        signed_payload = f"{ts}.{raw_body.decode('utf-8')}".encode()
        computed_sig = hmac.new(sec.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
        return ("Stripe-Signature", f"t={ts},v1={computed_sig}")
    elif gw == "razorpay":
        computed_sig = hmac.new(sec.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return ("X-Razorpay-Signature", computed_sig)
    elif gw == "payu":
        computed_sig = hmac.new(sec.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return ("X-PayU-Signature", computed_sig)
    elif gw == "upi":
        computed_sig = hmac.new(sec.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return ("X-UPI-Signature", computed_sig)
    else:
        computed_sig = hmac.new(sec.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return ("X-Signature", computed_sig)


# =============================================================================
# HMAC-SHA256 SIGNATURE VERIFICATION
# =============================================================================


def verify_hmac_signature(
    gateway: str,
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str | None = None,
) -> bool:
    """
    Verifies incoming webhook payload signature using HMAC-SHA256.
    Uses timingSafeEqual (hmac.compare_digest) for constant-time comparison
    to defend against timing attacks.

    Raises:
        MissingWebhookSignatureException if the signature header is absent.
        InvalidWebhookSignatureException if the signature is invalid.
    """
    gw = gateway.lower().strip()
    sec = get_gateway_webhook_secret(gw, secret)
    normalized_headers = {k.lower(): v for k, v in headers.items()}

    # 1. Locate relevant signature header
    candidate_headers = GATEWAY_SIGNATURE_HEADERS.get(gw, ["x-signature", "x-webhook-signature"])
    sig_header_val: str | None = None
    matched_header_name = candidate_headers[0]

    for candidate in candidate_headers:
        if candidate in normalized_headers:
            sig_header_val = normalized_headers[candidate]
            matched_header_name = candidate
            break

    if not sig_header_val:
        raise MissingWebhookSignatureException(gateway=gw, expected_header=matched_header_name)

    # 2. Gateway-specific signature extraction & calculation
    if gw == "stripe":
        # Stripe signature format: t={timestamp},v1={hash},v1={hash2}...
        # Also support plain hex if passed without t=
        if "t=" in sig_header_val and "v1=" in sig_header_val:
            timestamp: str | None = None
            candidate_sigs: list[str] = []
            for part in sig_header_val.split(","):
                part = part.strip()
                if part.startswith("t="):
                    timestamp = part[2:]
                elif part.startswith("v1="):
                    candidate_sigs.append(part[3:])

            if not timestamp or not candidate_sigs:
                raise InvalidWebhookSignatureException(gw, "Malformed Stripe-Signature header")

            signed_payload = f"{timestamp}.{raw_body.decode('utf-8')}".encode()
            expected_sig = hmac.new(sec.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

            # Compare all v1 candidates in constant time
            matched = any(timingSafeEqual(expected_sig, cand) for cand in candidate_sigs)
            if not matched:
                raise InvalidWebhookSignatureException(gw, "Stripe HMAC signature mismatch")
            return True
        else:
            # Fallback for direct hex signature
            expected_sig = hmac.new(sec.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
            if not timingSafeEqual(expected_sig, sig_header_val.strip()):
                raise InvalidWebhookSignatureException(gw, "Stripe raw HMAC signature mismatch")
            return True

    elif gw in ("razorpay", "payu", "upi"):
        expected_sig = hmac.new(sec.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not timingSafeEqual(expected_sig, sig_header_val.strip()):
            raise InvalidWebhookSignatureException(gw, f"{gw.upper()} HMAC signature mismatch")
        return True

    else:
        # Generic fallback HMAC-SHA256
        expected_sig = hmac.new(sec.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not timingSafeEqual(expected_sig, sig_header_val.strip()):
            raise InvalidWebhookSignatureException(gw, f"Generic HMAC signature mismatch for {gw}")
        return True


# =============================================================================
# EVENT METADATA EXTRACTION
# =============================================================================


def extract_event_metadata(
    gateway: str,
    raw_body: bytes,
    headers: Mapping[str, str],
    parsed_payload: dict[str, Any],
) -> tuple[str, str]:
    """
    Extracts (event_id, event_type) from incoming webhook request.
    Handles nested gateway patterns and standardized envelope payloads.
    """
    gw = gateway.lower().strip()
    norm_headers = {k.lower(): v for k, v in headers.items()}

    # Check headers first
    header_event_id = (
        norm_headers.get("x-event-id")
        or norm_headers.get("x-webhook-id")
        or norm_headers.get("stripe-event-id")
    )

    # 1. Extract event_id
    event_id: str | None = None
    if parsed_payload.get("event_id"):
        event_id = str(parsed_payload["event_id"])
    elif header_event_id:
        event_id = str(header_event_id)
    elif gw == "stripe" and parsed_payload.get("id"):
        event_id = str(parsed_payload["id"])
    elif gw == "razorpay":
        # Razorpay event: id in event or entity id
        if parsed_payload.get("id"):
            event_id = str(parsed_payload["id"])
        elif "payload" in parsed_payload and isinstance(parsed_payload["payload"], dict):
            for entity_type in ("payment", "order", "refund"):
                entity = parsed_payload["payload"].get(entity_type, {}).get("entity", {})
                if "id" in entity:
                    event_id = str(entity["id"])
                    break
    elif gw == "payu":
        event_id = (
            str(
                parsed_payload.get("mihpayid")
                or parsed_payload.get("txnid")
                or parsed_payload.get("id")
                or ""
            )
            or None
        )
    elif gw == "upi":
        event_id = (
            str(
                parsed_payload.get("txn_id")
                or parsed_payload.get("npci_txn_id")
                or parsed_payload.get("id")
                or ""
            )
            or None
        )

    if not event_id and parsed_payload.get("id"):
        event_id = str(parsed_payload["id"])

    # Fallback to deterministic SHA-256 hash if no event_id was located
    if not event_id:
        event_id = f"evt_{hashlib.sha256(raw_body).hexdigest()[:24]}"

    # 2. Extract event_type
    event_type = (
        parsed_payload.get("event_type")
        or parsed_payload.get("type")
        or parsed_payload.get("event")
        or parsed_payload.get("status")
        or "payment.notification"
    )

    return str(event_id), str(event_type)


# =============================================================================
# WEBHOOK INGESTION PIPELINE & DLQ SERVICE
# =============================================================================

WebhookHandlerType = Callable[[str, str, dict[str, Any], AsyncSession], Awaitable[None]]


class WebhookIngestionService:
    """
    Industrial Webhook Ingestion Pipeline featuring:
    1. HMAC-SHA256 signature verification with timingSafeEqual (constant-time).
    2. Deduplication on composite primary key (gateway, event_id).
    3. Dead Letter Queue (DLQ) pattern: automatic DLQ transition after 3 failures.
    """

    MAX_RETRIES: int = 3

    def __init__(self):
        self._handlers: dict[str, WebhookHandlerType] = {}

    def register_handler(self, event_type: str, handler: WebhookHandlerType) -> None:
        """Registers custom domain handler for a specific event type."""
        self._handlers[event_type] = handler

    async def ingest_webhook(
        self,
        gateway: str,
        raw_body: bytes,
        headers: Mapping[str, str],
        db: AsyncSession,
        custom_handler: WebhookHandlerType | None = None,
        secret_override: str | None = None,
    ) -> dict[str, Any]:
        """
        Executes the end-to-end ingestion pipeline:
        Step 1: HMAC-SHA256 verification using timingSafeEqual.
        Step 2: Parse raw JSON and extract event metadata.
        Step 3: Deduplication check on composite PK (gateway, event_id).
                If already PROCESSED or DLQ, return HTTP 200 immediately.
        Step 4: Execute handler with retry tracking.
        Step 5: DLQ routing if 3 failures occur.
        """
        gateway_clean = gateway.lower().strip()
        now = datetime.now(UTC)

        # ---------------------------------------------------------------------
        # Step 1: Verify HMAC Signature (timingSafeEqual)
        # ---------------------------------------------------------------------
        verify_hmac_signature(
            gateway=gateway_clean,
            raw_body=raw_body,
            headers=headers,
            secret=secret_override,
        )

        # ---------------------------------------------------------------------
        # Step 2: Parse Payload & Extract Metadata
        # ---------------------------------------------------------------------
        try:
            parsed_payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            parsed_payload = {"raw_content": raw_body.decode("utf-8", errors="replace")}

        # Unwrap if body is wrapped in {"event_id":..., "event_type":..., "payload":{...}}
        unwrapped_payload = parsed_payload
        if (
            isinstance(parsed_payload, dict)
            and "payload" in parsed_payload
            and isinstance(parsed_payload["payload"], dict)
        ):
            unwrapped_payload = parsed_payload["payload"]

        event_id, event_type = extract_event_metadata(
            gateway_clean, raw_body, headers, parsed_payload
        )
        payload_hash = hashlib.sha256(raw_body).hexdigest()

        # ---------------------------------------------------------------------
        # Step 3: Deduplication Check via Composite Primary Key (gateway, event_id)
        # ---------------------------------------------------------------------
        query = select(ProcessedWebhookEvents).where(
            ProcessedWebhookEvents.gateway == gateway_clean,
            ProcessedWebhookEvents.event_id == event_id,
        )
        res = await db.execute(query)
        existing_event = res.scalar_one_or_none()

        if existing_event is not None:
            # 3A. Already successfully processed: Return HTTP 200 immediately!
            if existing_event.status == WebhookProcessingStatus.PROCESSED.value:
                logger.info(
                    "Deduplication: Webhook event (%s, %s) already PROCESSED. Returning HTTP 200 immediately.",
                    gateway_clean,
                    event_id,
                )
                return {
                    "status": "duplicate_ignored",
                    "gateway": gateway_clean,
                    "event_id": event_id,
                    "event_type": existing_event.event_type,
                    "message": "Duplicate webhook event already processed.",
                    "processed_at": (
                        existing_event.processed_at.isoformat()
                        if existing_event.processed_at
                        else None
                    ),
                    "retry_count": existing_event.retry_count,
                }

            # 3B. Already moved to Dead Letter Queue (DLQ): Return HTTP 200 immediately!
            if existing_event.status == WebhookProcessingStatus.DLQ.value:
                logger.warning(
                    "Deduplication: Webhook event (%s, %s) already in DLQ. Returning HTTP 200 immediately.",
                    gateway_clean,
                    event_id,
                )
                return {
                    "status": "dlq_acknowledged",
                    "gateway": gateway_clean,
                    "event_id": event_id,
                    "event_type": existing_event.event_type,
                    "message": "Webhook event is stored in Dead Letter Queue (DLQ).",
                    "retry_count": existing_event.retry_count,
                    "error": existing_event.processing_error,
                }

            # 3C. Event exists in FAILED status: Retrying webhook processing
            return await self._process_retry_attempt(
                event=existing_event,
                gateway=gateway_clean,
                event_type=event_type,
                payload=unwrapped_payload,
                db=db,
                custom_handler=custom_handler,
            )

        # ---------------------------------------------------------------------
        # Step 4: First-time Ingestion (status=RECEIVED, retry_count=0)
        # ---------------------------------------------------------------------
        new_event = ProcessedWebhookEvents(
            gateway=gateway_clean,
            event_id=event_id,
            event_type=event_type,
            payload_hash=payload_hash,
            status=WebhookProcessingStatus.RECEIVED.value,
            retry_count=0,
            raw_payload=parsed_payload,
            processing_error=None,
            received_at=now,
            processed_at=None,
        )
        db.add(new_event)
        await db.commit()
        await db.refresh(new_event)

        # ---------------------------------------------------------------------
        # Step 5: Execute Processing with DLQ Fallback on Failure
        # ---------------------------------------------------------------------
        handler_to_run = (
            custom_handler or self._handlers.get(event_type) or self._default_transaction_processor
        )

        try:
            await handler_to_run(gateway_clean, event_type, unwrapped_payload, db)
            new_event.status = WebhookProcessingStatus.PROCESSED.value
            new_event.processed_at = datetime.now(UTC)
            new_event.processing_error = None
            await db.commit()

            return {
                "status": WebhookProcessingStatus.PROCESSED.value,
                "gateway": gateway_clean,
                "event_id": event_id,
                "event_type": event_type,
                "retry_count": new_event.retry_count,
                "message": "Webhook processed successfully.",
            }

        except Exception as exc:
            logger.error(
                "Webhook processing failed for (%s, %s): %s",
                gateway_clean,
                event_id,
                str(exc),
                exc_info=True,
            )
            new_event.retry_count = 1
            new_event.processing_error = str(exc)

            if new_event.retry_count >= self.MAX_RETRIES:
                new_event.status = WebhookProcessingStatus.DLQ.value
                new_event.processed_at = datetime.now(UTC)
                await db.commit()
                logger.critical(
                    "[DLQ_ALERT] Webhook (%s, %s) moved to DLQ after %d failed attempts",
                    gateway_clean,
                    event_id,
                    new_event.retry_count,
                )
                return {
                    "status": WebhookProcessingStatus.DLQ.value,
                    "gateway": gateway_clean,
                    "event_id": event_id,
                    "event_type": event_type,
                    "retry_count": new_event.retry_count,
                    "error": str(exc),
                    "message": "Webhook moved to DLQ after reaching maximum retries.",
                }

            new_event.status = WebhookProcessingStatus.FAILED.value
            await db.commit()
            raise WebhookProcessingException(
                f"Webhook processing failed (attempt {new_event.retry_count}/{self.MAX_RETRIES}): {exc!s}"
            )

    async def _process_retry_attempt(
        self,
        event: ProcessedWebhookEvents,
        gateway: str,
        event_type: str,
        payload: dict[str, Any],
        db: AsyncSession,
        custom_handler: WebhookHandlerType | None = None,
    ) -> dict[str, Any]:
        """
        Processes a retried webhook event, managing the retry count
        and transitioning to DLQ if failures reach 3.
        """
        event.retry_count += 1
        now = datetime.now(UTC)

        # Check if retry limit exceeded before attempt
        if event.retry_count >= self.MAX_RETRIES:
            event.status = WebhookProcessingStatus.DLQ.value
            event.processed_at = now
            event.processing_error = f"Exceeded maximum retries ({self.MAX_RETRIES}). Last error: {event.processing_error}"
            await db.commit()
            logger.critical(
                "[DLQ_ALERT] Webhook (%s, %s) permanently moved to DLQ after %d failed attempts",
                gateway,
                event.event_id,
                event.retry_count,
            )
            return {
                "status": WebhookProcessingStatus.DLQ.value,
                "gateway": gateway,
                "event_id": event.event_id,
                "event_type": event.event_type,
                "retry_count": event.retry_count,
                "message": "Webhook moved to Dead Letter Queue (DLQ).",
                "error": event.processing_error,
            }

        # Attempt retry execution
        handler_to_run = (
            custom_handler or self._handlers.get(event_type) or self._default_transaction_processor
        )
        try:
            await handler_to_run(gateway, event_type, payload, db)
            event.status = WebhookProcessingStatus.PROCESSED.value
            event.processed_at = now
            event.processing_error = None
            await db.commit()

            return {
                "status": WebhookProcessingStatus.PROCESSED.value,
                "gateway": gateway,
                "event_id": event.event_id,
                "event_type": event.event_type,
                "retry_count": event.retry_count,
                "message": "Webhook processed successfully on retry.",
            }

        except Exception as exc:
            logger.error(
                "Retry failed for (%s, %s) on attempt %d: %s",
                gateway,
                event.event_id,
                event.retry_count,
                str(exc),
            )
            event.processing_error = str(exc)

            if event.retry_count >= self.MAX_RETRIES:
                event.status = WebhookProcessingStatus.DLQ.value
                event.processed_at = now
                await db.commit()
                logger.critical(
                    "[DLQ_ALERT] Webhook (%s, %s) moved to DLQ after %d failed attempts",
                    gateway,
                    event.event_id,
                    event.retry_count,
                )
                return {
                    "status": WebhookProcessingStatus.DLQ.value,
                    "gateway": gateway,
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "retry_count": event.retry_count,
                    "message": "Webhook moved to Dead Letter Queue (DLQ).",
                    "error": str(exc),
                }

            event.status = WebhookProcessingStatus.FAILED.value
            await db.commit()
            raise WebhookProcessingException(
                f"Webhook retry failed (attempt {event.retry_count}/{self.MAX_RETRIES}): {exc!s}"
            )

    async def _default_transaction_processor(
        self,
        gateway: str,
        event_type: str,
        payload: dict[str, Any],
        db: AsyncSession,
    ) -> None:
        """
        Default asynchronous business processor:
        If transaction_id is present in payload, automatically triggers deterministic
        state transitions via TransactionStateMachine using GATEWAY_WEBHOOK trigger.
        """
        txn_id = (
            payload.get("transaction_id")
            or payload.get("metadata", {}).get("transaction_id")
            or payload.get("notes", {}).get("transaction_id")
        )

        if not txn_id:
            # No transaction attached; standard notification acknowledged
            return

        from app.services.state_machine import TransactionStateMachine

        ev_lower = event_type.lower()
        if any(term in ev_lower for term in ("succeeded", "captured", "success")):
            await TransactionStateMachine.transition(
                transaction_id=txn_id,
                target_state=TransactionState.CAPTURED,
                trigger=StateTrigger.GATEWAY_WEBHOOK,
                db=db,
                metadata={"gateway": gateway, "event_type": event_type},
            )
        elif any(term in ev_lower for term in ("failed", "failure", "declined")):
            await TransactionStateMachine.transition(
                transaction_id=txn_id,
                target_state=TransactionState.FAILED,
                trigger=StateTrigger.GATEWAY_WEBHOOK,
                db=db,
                metadata={"gateway": gateway, "event_type": event_type},
            )
        elif any(term in ev_lower for term in ("refunded", "refund.created", "refund.processed")):
            await TransactionStateMachine.transition(
                transaction_id=txn_id,
                target_state=TransactionState.REFUNDED,
                trigger=StateTrigger.GATEWAY_WEBHOOK,
                db=db,
                metadata={"gateway": gateway, "event_type": event_type},
            )

    # -------------------------------------------------------------------------
    # DLQ MANAGEMENT & REPLAY METHODS
    # -------------------------------------------------------------------------

    async def get_dlq_events(
        self,
        db: AsyncSession,
        gateway: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ProcessedWebhookEvents]:
        """Queries events currently placed in the Dead Letter Queue."""
        stmt = select(ProcessedWebhookEvents).where(
            ProcessedWebhookEvents.status == WebhookProcessingStatus.DLQ.value
        )
        if gateway:
            stmt = stmt.where(ProcessedWebhookEvents.gateway == gateway.lower().strip())
        stmt = stmt.order_by(desc(ProcessedWebhookEvents.received_at)).limit(limit).offset(offset)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def replay_dlq_event(
        self,
        gateway: str,
        event_id: str,
        db: AsyncSession,
        custom_handler: WebhookHandlerType | None = None,
    ) -> ProcessedWebhookEvents:
        """
        Manually or programmatically replays a webhook event stored in the DLQ.
        Resets status to PROCESSED on success, or updates the error trace if replay fails.
        """
        gateway_clean = gateway.lower().strip()
        stmt = select(ProcessedWebhookEvents).where(
            ProcessedWebhookEvents.gateway == gateway_clean,
            ProcessedWebhookEvents.event_id == event_id,
        )
        res = await db.execute(stmt)
        event = res.scalar_one_or_none()

        if not event:
            raise ValueError(f"Webhook event ({gateway_clean}, {event_id}) not found.")

        handler = (
            custom_handler
            or self._handlers.get(event.event_type)
            or self._default_transaction_processor
        )
        payload = event.raw_payload
        if (
            isinstance(payload, dict)
            and "payload" in payload
            and isinstance(payload["payload"], dict)
        ):
            payload = payload["payload"]

        try:
            await handler(gateway_clean, event.event_type, payload, db)
            event.status = WebhookProcessingStatus.PROCESSED.value
            event.processed_at = datetime.now(UTC)
            event.processing_error = None
            await db.commit()
            return event
        except Exception as exc:
            event.retry_count += 1
            event.processing_error = f"DLQ Replay Failed: {exc!s}"
            await db.commit()
            raise WebhookProcessingException(f"DLQ replay failed: {exc!s}")


# Global singleton instance
webhook_ingestion_service = WebhookIngestionService()
