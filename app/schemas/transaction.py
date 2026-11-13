import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import (
    PaymentMethod,
    StateTrigger,
    TransactionState,
    TransactionStatus,
)


class TransactionTransitionRequest(BaseModel):
    """Payload to trigger an explicit state machine transition."""

    target_state: TransactionState
    trigger: StateTrigger
    actor_id: str = Field(..., max_length=100)
    gateway_payload: dict[str, Any] | None = None
    transition_metadata: dict[str, Any] | None = None
    gateway_id: str | None = None
    gateway_transaction_id: str | None = None
    gateway_reference_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class TransactionCreateRequest(BaseModel):
    """
    Client transaction initiation payload.
    STRICT RULE: Amount MUST be an integer representing minor currency units (paise/cents).
    """

    merchant_id: str = Field(..., max_length=100, examples=["merch_live_948271"])
    order_id: str = Field(..., max_length=100, examples=["ord_20261110_99182"])
    customer_id: str | None = Field(None, max_length=100, examples=["cust_882910"])

    # Financial amount in paise/cents (NEVER floats)
    amount: int = Field(
        ...,
        gt=0,
        description="Transaction amount in minor currency units (e.g. 50000 = ₹500.00 or $500.00). Must be BIGINT integer.",
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
        default_factory=dict, examples=[{"vpa": "user@okhdfcbank"}]
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
                "Floating-point amounts are strictly prohibited. Pass integer paise/cents."
            )
        if v <= 0:
            raise ValueError("Amount must be strictly positive.")
        return int(v)


class TransactionStateLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    transaction_id: uuid.UUID
    from_state: str | None
    to_state: str
    trigger: StateTrigger
    actor_id: str
    gateway_payload: dict[str, Any] | None = None
    transition_metadata: dict[str, Any] | None = None
    created_at: datetime


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    merchant_id: str
    order_id: str
    customer_id: str | None
    amount: int = Field(..., description="Amount in minor currency units (paise/cents)")
    currency: str
    fee_amount: int = Field(..., description="PSP fee in minor units")
    tax_amount: int = Field(..., description="Tax in minor units")
    refunded_amount: int = Field(..., description="Refunded minor units")
    status: TransactionStatus
    payment_method: str
    payment_method_details: dict[str, Any] | None = None
    gateway_id: str | None = None
    gateway_transaction_id: str | None = None
    gateway_reference_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    state_logs: list[TransactionStateLogResponse] | None = []
