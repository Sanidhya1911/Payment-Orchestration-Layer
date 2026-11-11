import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_db
from app.gateways.base import GatewayAuthRequest
from app.gateways.router import gateway_router
from app.models.gateway import GatewayConfig
from app.models.routing import GatewayRoutes, RoutingConfig
from app.schemas.gateway import (
    EvaluateRoutingRequest,
    EvaluateRoutingResponse,
    GatewayConfigCreate,
    GatewayConfigResponse,
    GatewayRouteCreate,
    GatewayScoreResponse,
    RoutingConfigCreate,
)

router = APIRouter()


@router.post("", response_model=GatewayConfigResponse, status_code=status.HTTP_201_CREATED)
async def register_gateway(
    payload: GatewayConfigCreate,
    db: AsyncSession = Depends(get_async_db),
):
    """Register or onboard a new payment service provider (PSP) gateway configuration."""
    existing = await db.get(GatewayConfig, payload.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Gateway with id '{payload.id}' already exists."
        )

    gateway = GatewayConfig(
        id=payload.id,
        name=payload.name,
        gateway_type=payload.gateway_type.value,
        status=payload.status.value,
        api_base_url=payload.api_base_url,
        credentials_vault_ref=payload.credentials_vault_ref,
        supported_currencies=payload.supported_currencies,
        supported_payment_methods=payload.supported_payment_methods,
        rate_limit_tps=payload.rate_limit_tps,
        timeout_ms=payload.timeout_ms,
        config_metadata=payload.config_metadata,
    )
    db.add(gateway)
    await db.flush()
    return gateway


@router.get("", response_model=List[GatewayConfigResponse])
async def list_gateways(
    db: AsyncSession = Depends(get_async_db),
):
    """Retrieve all onboarded payment gateways."""
    stmt = select(GatewayConfig).order_by(GatewayConfig.id)
    res = await db.execute(stmt)
    return res.scalars().all()


@router.post("/routing-configs", status_code=status.HTTP_201_CREATED)
async def create_routing_policy(
    payload: RoutingConfigCreate,
    db: AsyncSession = Depends(get_async_db),
):
    """Create a new smart dynamic routing policy configuration."""
    routing_cfg = RoutingConfig(
        merchant_id=payload.merchant_id,
        name=payload.name,
        is_active=payload.is_active,
        algorithm=payload.algorithm.value,
        failover_enabled=payload.failover_enabled,
        max_retry_attempts=payload.max_retry_attempts,
        circuit_breaker_error_threshold_pct=payload.circuit_breaker_error_threshold_pct,
        config_metadata=payload.config_metadata,
    )
    db.add(routing_cfg)
    await db.flush()
    return {"id": routing_cfg.id, "name": routing_cfg.name, "is_active": routing_cfg.is_active}


@router.post("/routes", status_code=status.HTTP_201_CREATED)
async def create_gateway_route(
    payload: GatewayRouteCreate,
    db: AsyncSession = Depends(get_async_db),
):
    """
    Create a specific gateway route rule.
    Validates BIGINT monetary constraints (min_amount / max_amount in minor units).
    """
    gateway = await db.get(GatewayConfig, payload.gateway_id)
    if not gateway:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Referenced gateway '{payload.gateway_id}' does not exist."
        )

    route = GatewayRoutes(
        routing_config_id=payload.routing_config_id,
        gateway_id=payload.gateway_id,
        priority=payload.priority,
        weight=payload.weight,
        currency=payload.currency,
        payment_method=payload.payment_method,
        card_network=payload.card_network,
        min_amount=payload.min_amount,  # BIGINT paise/cents
        max_amount=payload.max_amount,  # BIGINT paise/cents
        is_active=payload.is_active,
    )
    db.add(route)
    await db.flush()
    return {"id": route.id, "gateway_id": route.gateway_id, "priority": route.priority}


@router.post("/evaluate-routing", response_model=EvaluateRoutingResponse, status_code=status.HTTP_200_OK)
async def evaluate_dynamic_routes(
    payload: EvaluateRoutingRequest,
):
    """
    Evaluates candidate payment gateways using the multi-criteria dynamic scoring algorithm:
    Score = (W_success * NormSuccess)
          + (W_latency * (1 - NormLatency))
          + (W_cost * (1 - NormCost))
          + (W_health * HealthScore)
          + (W_fit * FitScore)
    Returns ranked gateways and the selected primary gateway.
    """
    mock_auth_req = GatewayAuthRequest(
        transaction_id=uuid.uuid4(),
        amount=payload.amount,
        currency=payload.currency,
        payment_method=payload.payment_method,
        merchant_id="merchant_eval",
        order_id="order_eval",
    )

    ranked = gateway_router.rank_gateways(
        request=mock_auth_req,
        candidate_ids=payload.candidate_gateways,
    )

    if not ranked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No candidate gateways matched routing criteria.",
        )

    score_items = [
        GatewayScoreResponse(
            gateway_id=s.gateway_id,
            total_score=round(s.total_score, 4),
            norm_success=round(s.norm_success, 4),
            norm_latency=round(s.norm_latency, 4),
            norm_cost=round(s.norm_cost, 4),
            health_score=round(s.health_score, 4),
            fit_score=round(s.fit_score, 4),
            circuit_status=s.circuit_status.value,
        )
        for s in ranked
    ]

    return EvaluateRoutingResponse(
        selected_primary_gateway=ranked[0].gateway_id,
        ranked_gateways=score_items,
    )


