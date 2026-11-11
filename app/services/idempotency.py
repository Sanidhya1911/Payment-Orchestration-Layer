import hashlib
import json
import logging
import struct
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, Optional, Tuple, Union
from fastapi import HTTPException, status
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import async_session_maker
from app.core.enums import IdempotencyStatus
from app.models.idempotency import IdempotencyKeys

logger = logging.getLogger("payment_layer.idempotency")

DEFAULT_IDEMPOTENCY_TTL = timedelta(hours=24)


# =============================================================================
# IDEMPOTENCY EXCEPTIONS
# =============================================================================
class InvalidIdempotencyKeyException(HTTPException):
    """Raised when the Idempotency-Key header is not a valid UUID."""
    def __init__(self, key: Optional[str] = None):
        super().__init__(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid Idempotency-Key header: '{key or 'MISSING'}'. "
                "The idempotency key must be a valid client-generated UUIDv4."
            ),
        )


class IdempotencyConflictException(HTTPException):
    """
    Raised when a duplicate request arrives while a previous request with the same
    idempotency key is actively PROCESSING. Returns HTTP 409 Conflict with Retry-After.
    """
    def __init__(self, key: str, retry_after_seconds: int = 2):
        self.key = key
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A request with idempotency key '{key}' is currently PROCESSING. "
                "Concurrent duplicate request rejected to prevent double-charging. "
                "Please retry shortly."
            ),
            headers={"Retry-After": str(retry_after_seconds)},
        )


class IdempotencyPayloadMismatchException(HTTPException):
    """
    Raised when an existing idempotency key is reused with a different request payload.
    Prevents parameter tampering and inconsistent replay semantics.
    """
    def __init__(self, key: str):
        self.key = key
        super().__init__(
            status_code=422,
            detail=(
                f"Idempotency key '{key}' was previously used with a different request payload. "
                "Reusing an idempotency key with altered request parameters is prohibited."
            ),
        )


def _ensure_utc(dt: datetime) -> datetime:
    """Guarantees datetime is timezone-aware in UTC across SQLite and PostgreSQL."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# =============================================================================
# DATA STRUCTURES
# =============================================================================
@dataclass
class IdempotencyResult:
    """Represents the resolution of an idempotency evaluation."""
    is_cached: bool
    response_code: Optional[int] = None
    response_body: Optional[Any] = None
    response_headers: Optional[Dict[str, Any]] = None
    key_record: Optional[IdempotencyKeys] = None


@dataclass
class IdempotencyScopeHandle:
    """Handle passed into the idempotent_scope context manager."""
    is_cached: bool
    cached_code: Optional[int] = None
    cached_response: Optional[Any] = None
    cached_headers: Optional[Dict[str, Any]] = None

    _response_code: Optional[int] = None
    _response_body: Optional[Any] = None
    _response_headers: Optional[Dict[str, Any]] = None

    def set_response(
        self,
        status_code: int,
        body: Any,
        headers: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Stores the response to be cached for subsequent idempotent invocations."""
        self._response_code = status_code
        self._response_body = body
        self._response_headers = headers or {}


# =============================================================================
# IDEMPOTENCY & DISTRIBUTED LOCKING SERVICE
# =============================================================================
class IdempotencyService:
    """
    High-performance, mission-critical idempotency and distributed locking engine.
    
    Architectural Guarantees:
    1. Enforces client-generated UUID format for Idempotency-Key.
    2. Uses PostgreSQL advisory locks (pg_advisory_xact_lock) on a 64-bit hash
       of the idempotency key to prevent concurrent race conditions.
    3. Status 'PROCESSING' returns HTTP 409 Conflict with Retry-After header.
    4. Status 'COMPLETED' returns cached JSON response immediately.
    5. Automatic 24-hour expiration (TTL) and background cleanup capabilities.
    """

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession] = async_session_maker,
        ttl: timedelta = DEFAULT_IDEMPOTENCY_TTL,
    ):
        self._session_maker = session_maker
        self.ttl = ttl

    @classmethod
    def validate_key(cls, key: Optional[str]) -> str:
        """
        Validates that the provided idempotency key is a non-empty, valid UUID.
        Raises InvalidIdempotencyKeyException if validation fails.
        """
        if not key or not str(key).strip():
            raise InvalidIdempotencyKeyException("EMPTY")
        try:
            parsed = uuid.UUID(str(key).strip())
            return str(parsed)
        except (ValueError, TypeError, AttributeError) as exc:
            raise InvalidIdempotencyKeyException(key) from exc

    @classmethod
    def compute_payload_hash(cls, payload: Any) -> str:
        """
        Generates a deterministic SHA-256 hash of the canonicalized request payload.
        Ensures dictionary key ordering and consistent JSON serialization.
        """
        if payload is None:
            canonical_json = ""
        elif isinstance(payload, str):
            canonical_json = payload.strip()
        else:
            canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    @classmethod
    def compute_advisory_lock_id(cls, key: str, merchant_id: str = "") -> int:
        """
        Calculates a signed 64-bit integer lock ID from the idempotency key and merchant ID.
        PostgreSQL's pg_advisory_xact_lock requires a signed 64-bit bigint (-2^63 to 2^63 - 1).
        """
        canonical = f"{merchant_id.strip()}:{key.strip()}" if merchant_id else key.strip()
        digest = hashlib.sha256(canonical.encode("utf-8")).digest()
        # Unpack first 8 bytes as big-endian signed 64-bit integer
        (lock_id,) = struct.unpack(">q", digest[:8])
        return lock_id

    async def acquire_advisory_lock(self, session: AsyncSession, lock_id: int) -> None:
        """
        Executes pg_advisory_xact_lock(bigint).
        The advisory lock is held exclusively for the duration of the current SQL transaction,
        and is released automatically when the transaction commits or rolls back.
        """
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": lock_id}
        )
        logger.debug("PostgreSQL transaction advisory lock acquired for lock_id=%s", lock_id)

    async def process_or_acquire(
        self,
        session: AsyncSession,
        key: str,
        merchant_id: str,
        request_path: str,
        payload: Any,
    ) -> IdempotencyResult:
        """
        Core idempotency verification step executed under an advisory transaction lock:
        - If key does not exist: creates a record with status='PROCESSING'.
        - If key exists & status='COMPLETED': returns cached response.
        - If key exists & status='PROCESSING': raises HTTP 409 Conflict.
        - If key exists & payload altered: raises HTTP 422 Unprocessable Entity.
        """
        valid_key = self.validate_key(key)
        payload_hash = self.compute_payload_hash(payload)
        now = datetime.now(timezone.utc)

        # 1. Acquire transaction-level PostgreSQL advisory lock
        lock_id = self.compute_advisory_lock_id(valid_key, merchant_id)
        await self.acquire_advisory_lock(session, lock_id)

        # 2. Lookup existing idempotency record
        query = select(IdempotencyKeys).where(
            IdempotencyKeys.key == valid_key,
            IdempotencyKeys.merchant_id == merchant_id,
        )
        res = await session.execute(query)
        record = res.scalar_one_or_none()

        if record is not None:
            # 3. Check for TTL Expiration (24 hours)
            record_expiry = _ensure_utc(record.expires_at)
            if record_expiry <= now:
                logger.info("Idempotency key %s expired at %s. Resetting as new key.", valid_key, record.expires_at)
                await session.delete(record)
                await session.flush()
                record = None

        if record is not None:
            # 4. Tampering Protection: Ensure payload has not changed
            if record.request_hash != payload_hash:
                logger.warning(
                    "Idempotency payload tampering detected for key %s (stored: %s, received: %s)",
                    valid_key,
                    record.request_hash,
                    payload_hash,
                )
                raise IdempotencyPayloadMismatchException(valid_key)

            # 5. Concurrent In-Flight Check: Status is PROCESSING -> 409 Conflict
            if record.status == IdempotencyStatus.PROCESSING.value:
                logger.warning("Concurrent duplicate request detected for key %s (status=PROCESSING)", valid_key)
                raise IdempotencyConflictException(valid_key)

            # 6. Idempotent Success: Status is COMPLETED -> Return Cached JSON Response
            if record.status == IdempotencyStatus.COMPLETED.value:
                logger.info("Returning cached idempotent response for key %s (HTTP %s)", valid_key, record.response_code)
                return IdempotencyResult(
                    is_cached=True,
                    response_code=record.response_code or status.HTTP_200_OK,
                    response_body=record.response_body,
                    response_headers=record.response_headers,
                    key_record=record,
                )

            # 7. Previous Failure: Allow retry by resetting to PROCESSING
            if record.status == IdempotencyStatus.FAILED.value:
                logger.info("Retrying previously failed request for key %s", valid_key)
                record.status = IdempotencyStatus.PROCESSING.value
                record.request_path = request_path
                record.response_code = None
                record.response_body = None
                record.response_headers = None
                record.expires_at = now + self.ttl
                await session.flush()
                return IdempotencyResult(is_cached=False, key_record=record)

        # 8. New Request: Insert record in status 'PROCESSING' with 24h expiration
        expires_at = now + self.ttl
        new_record = IdempotencyKeys(
            key=valid_key,
            merchant_id=merchant_id,
            request_path=request_path,
            request_hash=payload_hash,
            status=IdempotencyStatus.PROCESSING.value,
            locked_until=now + timedelta(seconds=60),
            expires_at=expires_at,
        )
        session.add(new_record)
        await session.flush()

        logger.info("Registered new idempotency key %s (status=PROCESSING, expires_at=%s)", valid_key, expires_at)
        return IdempotencyResult(is_cached=False, key_record=new_record)

    async def save_response(
        self,
        session: AsyncSession,
        key: str,
        merchant_id: str,
        response_code: int,
        response_body: Any,
        response_headers: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Transition idempotency record to 'COMPLETED' and cache the response payload.
        """
        valid_key = self.validate_key(key)
        query = select(IdempotencyKeys).where(
            IdempotencyKeys.key == valid_key,
            IdempotencyKeys.merchant_id == merchant_id,
        )
        res = await session.execute(query)
        record = res.scalar_one_or_none()

        if record is not None:
            record.status = IdempotencyStatus.COMPLETED.value
            record.response_code = response_code
            record.response_body = response_body
            record.response_headers = response_headers or {}
            record.locked_until = None
            await session.flush()
            logger.info("Idempotency key %s transitioned to COMPLETED (status_code=%s)", valid_key, response_code)

    async def mark_failed(
        self,
        session: AsyncSession,
        key: str,
        merchant_id: str,
        error_details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Transitions idempotency record to 'FAILED' upon unexpected processing error.
        """
        valid_key = self.validate_key(key)
        query = select(IdempotencyKeys).where(
            IdempotencyKeys.key == valid_key,
            IdempotencyKeys.merchant_id == merchant_id,
        )
        res = await session.execute(query)
        record = res.scalar_one_or_none()

        if record is not None:
            record.status = IdempotencyStatus.FAILED.value
            record.response_body = error_details or {"error": "Processing failed"}
            record.locked_until = None
            await session.flush()
            logger.warning("Idempotency key %s marked as FAILED", valid_key)

    async def cleanup_expired_keys(self, session: Optional[AsyncSession] = None) -> int:
        """
        Expires and cleans up idempotency keys after 24 hours.
        Deletes all rows where expires_at <= current UTC timestamp.
        Returns the number of pruned records.
        """
        now = datetime.now(timezone.utc)
        stmt = delete(IdempotencyKeys).where(IdempotencyKeys.expires_at <= now)

        if session is not None:
            res = await session.execute(stmt)
            deleted_count = res.rowcount
            await session.flush()
            logger.info("Pruned %d expired idempotency keys (expired <= %s)", deleted_count, now)
            return deleted_count
        else:
            async with self._session_maker() as sess:
                async with sess.begin():
                    res = await sess.execute(stmt)
                    deleted_count = res.rowcount
                logger.info("Pruned %d expired idempotency keys (expired <= %s)", deleted_count, now)
                return deleted_count

    @asynccontextmanager
    async def idempotent_scope(
        self,
        session: AsyncSession,
        key: Optional[str],
        merchant_id: str,
        request_path: str,
        payload: Any,
    ) -> AsyncIterator[IdempotencyScopeHandle]:
        """
        Context manager for orchestrating idempotent operations:
        - If key is None: yields un-cached handle (bypasses idempotency).
        - If key is provided: enforces advisory lock, checks PROCESSING (409) / COMPLETED (cached).
        - Upon success: saves response body and status code to cache.
        - Upon exception: marks record as FAILED.
        """
        if not key:
            # Idempotency header omitted; execute normally without caching
            yield IdempotencyScopeHandle(is_cached=False)
            return

        valid_key = self.validate_key(key)
        result = await self.process_or_acquire(
            session=session,
            key=valid_key,
            merchant_id=merchant_id,
            request_path=request_path,
            payload=payload,
        )

        if result.is_cached:
            handle = IdempotencyScopeHandle(
                is_cached=True,
                cached_code=result.response_code,
                cached_response=result.response_body,
                cached_headers=result.response_headers,
            )
            yield handle
            return

        handle = IdempotencyScopeHandle(is_cached=False)
        try:
            yield handle
            if handle._response_code is not None:
                await self.save_response(
                    session=session,
                    key=valid_key,
                    merchant_id=merchant_id,
                    response_code=handle._response_code,
                    response_body=handle._response_body,
                    response_headers=handle._response_headers,
                )
        except Exception as exc:
            await self.mark_failed(
                session=session,
                key=valid_key,
                merchant_id=merchant_id,
                error_details={"error_type": type(exc).__name__, "message": str(exc)},
            )
            raise exc


# Singleton instance
idempotency_service = IdempotencyService()


def get_idempotency_service() -> IdempotencyService:
    """FastAPI dependency for accessing the IdempotencyService."""
    return idempotency_service
