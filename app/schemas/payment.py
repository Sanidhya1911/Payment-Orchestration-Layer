import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from app.core.enums import PaymentMethod


class PaymentCreateRequest(BaseModel):
    """
    Standardized payload for initiating a payment through the orchestration layer.
    STRICT FINANCIAL INVARIANT: Amount must be an integer representing minor units (paise/cents).
    """

    merchant_id: str = Field(..., max_length=100, examples=["merch_live_948271"])
    order_id: str = Field(..., max_length=100, examples=["ord_20261113_1109"])
    customer_id: str | None = Field(None, max_length=100, examples=["cust_882910"])

    # Strict BIGINT minor units: paise / cents
    amount: int = Field(
        ...,
        gt=0,
        description="Payment amount in minor currency units (e.g. 50000 = ₹500.00). Must be integer, never float.",
        examples=[50000],
    )
    currency: str = Field(
        ...,
        min_length=3,
        max_length=3,
        description="ISO 4217 Currency Code",
        examples=["INR"],
    )
    payment_method: PaymentMethod = Field(..., examples=[PaymentMethod.UPI])
    payment_method_details: dict[str, Any] | None = Field(
        default_factory=dict,
        examples=[{"vpa": "customer@okhdfcbank"}],
    )
    preferred_gateway: str | None = Field(
        None,
        description="Optional preferred gateway override (e.g. upi, razorpay, stripe, payu)",
        examples=["upi"],
    )
    auto_capture: bool = Field(
        default=True,
        description="If True, captures automatically upon authorization",
    )
    metadata: dict[str, Any] | None = Field(default_factory=dict)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, v: str) -> str:
        return v.upper()

    @field_validator("amount")
    @classmethod
    def validate_amount_not_float(cls, v: Any) -> int:
        if isinstance(v, float):
            raise ValueError(
                "CRITICAL FINANCIAL ERROR: Floating-point amounts are strictly prohibited. "
                "Specify integer minor units (paise/cents/pence)."
            )
        return int(v)


class PaymentCaptureRequest(BaseModel):
    """Payload to capture an authorized payment transaction."""

    amount: int | None = Field(
        None,
        gt=0,
        description="Amount to capture in minor units. Defaults to full authorized amount if omitted.",
    )
    currency: str | None = Field(None, min_length=3, max_length=3)

    @field_validator("amount")
    @classmethod
    def validate_amount_not_float(cls, v: Any) -> int | None:
        if isinstance(v, float):
            raise ValueError("Floating-point amounts prohibited. Specify integer minor units.")
        return int(v) if v is not None else None


class PaymentRefundRequest(BaseModel):
    """Payload to refund settled funds."""

    amount: int | None = Field(
        None,
        gt=0,
        description="Amount to refund in minor units. Defaults to remaining refundable balance if omitted.",
    )
    reason: str | None = Field(None, max_length=255, examples=["Customer requested cancellation"])

    @field_validator("amount")
    @classmethod
    def validate_amount_not_float(cls, v: Any) -> int | None:
        if isinstance(v, float):
            raise ValueError("Floating-point amounts prohibited. Specify integer minor units.")
        return int(v) if v is not None else None


class PaymentResponse(BaseModel):
    """Unified API response model for payment entities."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    merchant_id: str
    order_id: str
    customer_id: str | None = None
    amount: int
    currency: str
    status: str
    payment_method: str
    gateway_id: str | None = None
    gateway_transaction_id: str | None = None
    gateway_reference_id: str | None = None
    refunded_amount: int = 0
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] | None = Field(default=None, validation_alias="metadata_")

    @computed_field
    @property
    def captured_amount(self) -> int:
        if self.status in (
            "CAPTURED",
            "REFUNDED",
            "REFUND_INITIATED",
            "PARTIALLY_CAPTURED",
        ):
            return self.amount
        return 0
