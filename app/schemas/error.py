from typing import Any

from pydantic import BaseModel, Field


class ErrorDetails(BaseModel):
    gateway_error_code: str | None = Field(
        default=None,
        description="Downstream gateway error code if failure originated from a PSP",
    )
    extra: dict[str, Any] | None = None


class ErrorPayload(BaseModel):
    code: str = Field(..., description="Standardized machine-readable error code")
    message: str = Field(..., description="Human-readable explanation of error")
    details: ErrorDetails = Field(default_factory=ErrorDetails)


class UniformErrorResponse(BaseModel):
    """
    Uniform Error Envelope formatted strictly with:
    error.code, error.message, and details.gateway_error_code.
    """

    error: ErrorPayload
    detail: str | None = None  # Preserves legacy detail compatibility
