import logging
from typing import Any, Dict, Optional
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.gateways.exceptions import (
    CircuitBreakerOpenException,
    GatewayDeclinedException,
    GatewayException,
    GatewayServiceUnavailableException,
    GatewayTimeoutException,
)
from app.services.idempotency import (
    IdempotencyConflictException,
    IdempotencyPayloadMismatchException,
    InvalidIdempotencyKeyException,
)
from app.services.state_machine import (
    InvalidStateTransitionException,
    TransactionNotFoundException,
)

logger = logging.getLogger("payment_layer.errors")


class PaymentAPIException(HTTPException):
    """
    Standardized API exception structured strictly for uniform error formatting:
    error.code, error.message, details.gateway_error_code.
    """
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        gateway_error_code: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ):
        self.code = code
        self.message = message
        self.gateway_error_code = gateway_error_code
        self.extra = extra or {}
        payload = format_uniform_error_dict(
            code=code,
            message=message,
            gateway_error_code=gateway_error_code,
            extra=extra,
        )
        super().__init__(status_code=status_code, detail=payload)


def format_uniform_error_dict(
    code: str,
    message: str,
    gateway_error_code: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Constructs the uniform error dictionary according to strict specification:
    {
        "error": {
            "code": code,
            "message": message,
            "details": {
                "gateway_error_code": gateway_error_code
            }
        },
        "detail": message
    }
    """
    details: Dict[str, Any] = {"gateway_error_code": gateway_error_code}
    if extra:
        details["extra"] = extra

    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
        },
        "detail": message,  # Preserves legacy FastAPI detail inspectability
    }


def register_exception_handlers(app: FastAPI) -> None:
    """Registers unified exception handlers on the FastAPI application."""

    @app.exception_handler(PaymentAPIException)
    async def payment_api_exception_handler(request: Request, exc: PaymentAPIException):
        return JSONResponse(
            status_code=exc.status_code,
            content=format_uniform_error_dict(
                code=exc.code,
                message=exc.message,
                gateway_error_code=exc.gateway_error_code,
                extra=exc.extra,
            ),
        )

    @app.exception_handler(GatewayDeclinedException)
    async def gateway_declined_handler(request: Request, exc: GatewayDeclinedException):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=format_uniform_error_dict(
                code="PAYMENT_DECLINED",
                message=exc.message,
                gateway_error_code=exc.decline_code or "CARD_DECLINED",
                extra={"gateway_id": exc.gateway_id},
            ),
        )

    @app.exception_handler(GatewayTimeoutException)
    async def gateway_timeout_handler(request: Request, exc: GatewayTimeoutException):
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content=format_uniform_error_dict(
                code="GATEWAY_TIMEOUT",
                message=exc.message,
                gateway_error_code="TIMEOUT",
                extra={"gateway_id": exc.gateway_id},
            ),
        )

    @app.exception_handler(CircuitBreakerOpenException)
    async def circuit_breaker_handler(request: Request, exc: CircuitBreakerOpenException):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=format_uniform_error_dict(
                code="CIRCUIT_BREAKER_OPEN",
                message=exc.message,
                gateway_error_code="CIRCUIT_OPEN_FAST_FAIL",
                extra={"gateway_id": exc.gateway_id},
            ),
        )

    @app.exception_handler(GatewayServiceUnavailableException)
    async def gateway_unavailable_handler(request: Request, exc: GatewayServiceUnavailableException):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=format_uniform_error_dict(
                code="GATEWAY_UNAVAILABLE",
                message=exc.message,
                gateway_error_code="SERVICE_UNAVAILABLE",
                extra={"gateway_id": exc.gateway_id},
            ),
        )

    @app.exception_handler(GatewayException)
    async def gateway_generic_handler(request: Request, exc: GatewayException):
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content=format_uniform_error_dict(
                code="GATEWAY_ERROR",
                message=exc.message,
                gateway_error_code=exc.error_code or "GATEWAY_ERROR",
                extra={"gateway_id": exc.gateway_id},
            ),
        )

    @app.exception_handler(InvalidStateTransitionException)
    async def state_transition_handler(request: Request, exc: InvalidStateTransitionException):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=format_uniform_error_dict(
                code="INVALID_STATE_TRANSITION",
                message=str(exc),
                gateway_error_code=None,
                extra={
                    "current_state": exc.current_state,
                    "target_state": exc.target_state,
                    "transaction_id": exc.transaction_id,
                },
            ),
        )

    @app.exception_handler(TransactionNotFoundException)
    async def txn_not_found_handler(request: Request, exc: TransactionNotFoundException):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=format_uniform_error_dict(
                code="TRANSACTION_NOT_FOUND",
                message=str(exc),
                gateway_error_code=None,
                extra={"transaction_id": getattr(exc, "transaction_id", None)},
            ),
        )

    @app.exception_handler(IdempotencyConflictException)
    async def idemp_conflict_handler(request: Request, exc: IdempotencyConflictException):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            headers={"Retry-After": "2"},
            content=format_uniform_error_dict(
                code="IDEMPOTENCY_CONFLICT",
                message=exc.detail if isinstance(exc.detail, str) else "Concurrent request with same idempotency key in progress.",
                gateway_error_code=None,
            ),
        )

    @app.exception_handler(IdempotencyPayloadMismatchException)
    async def idemp_mismatch_handler(request: Request, exc: IdempotencyPayloadMismatchException):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=format_uniform_error_dict(
                code="IDEMPOTENCY_PAYLOAD_MISMATCH",
                message=exc.detail if isinstance(exc.detail, str) else "Idempotency key reused with altered payload.",
                gateway_error_code=None,
            ),
        )

    @app.exception_handler(InvalidIdempotencyKeyException)
    async def idemp_invalid_handler(request: Request, exc: InvalidIdempotencyKeyException):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=format_uniform_error_dict(
                code="INVALID_IDEMPOTENCY_KEY",
                message=exc.detail if isinstance(exc.detail, str) else "Invalid Idempotency-Key UUID header.",
                gateway_error_code=None,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=format_uniform_error_dict(
                code="VALIDATION_ERROR",
                message=f"Request schema validation failed: {str(exc.errors())}",
                gateway_error_code=None,
                extra={"errors": exc.errors()},
            ),
        )

    @app.exception_handler(HTTPException)
    async def generic_http_handler(request: Request, exc: HTTPException):
        # If detail is already formatted uniform dict, return it
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            content = exc.detail
        else:
            msg = str(exc.detail)
            code_map = {
                400: "BAD_REQUEST",
                401: "UNAUTHORIZED",
                403: "FORBIDDEN",
                404: "NOT_FOUND",
                409: "CONFLICT",
                422: "UNPROCESSABLE_ENTITY",
                500: "INTERNAL_SERVER_ERROR",
                502: "BAD_GATEWAY",
                503: "SERVICE_UNAVAILABLE",
                504: "GATEWAY_TIMEOUT",
            }
            code = code_map.get(exc.status_code, "ERROR")
            content = format_uniform_error_dict(
                code=code,
                message=msg,
                gateway_error_code=None,
            )

        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content=content,
        )

