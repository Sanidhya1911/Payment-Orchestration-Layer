import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import (
    GatewayStatus,
    GatewayType,
    RoutingAlgorithm,
)


class GatewayConfigCreate(BaseModel):
    id: str = Field(..., max_length=50, examples=["stripe_us"])
    name: str = Field(..., max_length=100, examples=["Stripe Primary US"])
    gateway_type: GatewayType = Field(..., examples=[GatewayType.STRIPE])
    status: GatewayStatus = Field(default=GatewayStatus.ACTIVE)
    api_base_url: str = Field(..., max_length=255, examples=["https://api.stripe.com/v1"])
    credentials_vault_ref: str = Field(
        ...,
        max_length=255,
        examples=["arn:aws:secretsmanager:us-east-1:123456789012:secret:stripe_prod-aB91k"],
    )
    supported_currencies: list[str] = Field(default=["INR", "USD", "EUR"])
    supported_payment_methods: list[str] = Field(default=["CARD", "UPI"])
    rate_limit_tps: int = Field(default=100, gt=0)
    timeout_ms: int = Field(default=5000, gt=0)
    config_metadata: dict[str, Any] | None = Field(default_factory=dict)


class GatewayConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    gateway_type: str
    status: str
    api_base_url: str
    credentials_vault_ref: str
    supported_currencies: list[str]
    supported_payment_methods: list[str]
    rate_limit_tps: int
    timeout_ms: int
    created_at: datetime
    updated_at: datetime


class RoutingConfigCreate(BaseModel):
    merchant_id: str | None = Field(None, max_length=100)
    name: str = Field(..., max_length=100, examples=["Standard Global Priority Routing"])
    is_active: bool = Field(default=True)
    algorithm: RoutingAlgorithm = Field(default=RoutingAlgorithm.PRIORITY)
    failover_enabled: bool = Field(default=True)
    max_retry_attempts: int = Field(default=2, ge=0, le=5)
    circuit_breaker_error_threshold_pct: int = Field(default=20, ge=1, le=100)
    config_metadata: dict[str, Any] | None = Field(default_factory=dict)


class GatewayRouteCreate(BaseModel):
    routing_config_id: uuid.UUID | None = None
    gateway_id: str = Field(..., max_length=50)
    priority: int = Field(default=1, ge=1)
    weight: int = Field(default=100, ge=0, le=100)
    currency: str | None = Field(None, min_length=3, max_length=3)
    payment_method: str | None = Field(None, max_length=50)
    card_network: str | None = Field(None, max_length=50)

    # STRICT RULE: BIGINT amounts in paise/cents, NEVER floats!
    min_amount: int | None = Field(
        None, gt=0, description="Minimum amount threshold in paise/cents (BIGINT)"
    )
    max_amount: int | None = Field(
        None, gt=0, description="Maximum amount threshold in paise/cents (BIGINT)"
    )
    is_active: bool = Field(default=True)

    @field_validator("min_amount", "max_amount")
    @classmethod
    def validate_amount_not_float(cls, v: Any) -> int | None:
        if v is None:
            return None
        if isinstance(v, float):
            raise ValueError(
                "Floating-point amounts are strictly prohibited. Pass integer paise/cents."
            )
        return int(v)


class GatewayHealthMetricsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    gateway_id: str
    window_start: datetime
    window_end: datetime
    total_requests: int
    successful_requests: int
    failed_requests: int
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    p99_latency_ms: int | None
    circuit_breaker_status: str
    last_heartbeat_at: datetime
    created_at: datetime


class EvaluateRoutingRequest(BaseModel):
    amount: int = Field(..., gt=0, description="Amount in minor units (BIGINT)")
    currency: str = Field(..., min_length=3, max_length=3, examples=["INR"])
    payment_method: str = Field(..., examples=["UPI"])
    candidate_gateways: list[str] | None = None


class GatewayScoreResponse(BaseModel):
    gateway_id: str
    total_score: float
    norm_success: float
    norm_latency: float
    norm_cost: float
    health_score: float
    fit_score: float
    circuit_status: str


class EvaluateRoutingResponse(BaseModel):
    selected_primary_gateway: str
    ranked_gateways: list[GatewayScoreResponse]
