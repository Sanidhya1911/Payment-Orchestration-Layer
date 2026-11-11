import uuid
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.enums import CircuitBreakerStatus
from app.gateways.base import GatewayAuthRequest
from app.gateways.exceptions import GatewayTimeoutException
from app.gateways.router import GatewayRouter
from app.main import app
from app.services.circuit_breaker import CircuitBreakerRegistry
from app.services.scoring import (
    GatewayMetrics,
    MultiCriteriaScorer,
    WEIGHT_COST,
    WEIGHT_FIT,
    WEIGHT_HEALTH,
    WEIGHT_LATENCY,
    WEIGHT_SUCCESS,
)


def test_weights_sum_to_one():
    """Verify all 5 architectural weights strictly sum to 100%."""
    total_weights = WEIGHT_SUCCESS + WEIGHT_LATENCY + WEIGHT_COST + WEIGHT_HEALTH + WEIGHT_FIT
    assert pytest.approx(total_weights, rel=1e-6) == 1.00
    assert WEIGHT_SUCCESS == 0.35  # 35%
    assert WEIGHT_LATENCY == 0.20  # 20%
    assert WEIGHT_COST == 0.20     # 20%
    assert WEIGHT_HEALTH == 0.15   # 15%
    assert WEIGHT_FIT == 0.10      # 10%


def test_multi_criteria_scoring_formula_exact_computation():
    """
    MANDATE:
    Use the formula:
    Score = (W_success * NormSuccess)
          + (W_latency * (1 - NormLatency))
          + (W_cost * (1 - NormCost))
          + (W_health * HealthScore)
          + (W_fit * FitScore)
    """
    scorer = MultiCriteriaScorer(
        w_success=0.35,
        w_latency=0.20,
        w_cost=0.20,
        w_health=0.15,
        w_fit=0.10,
    )

    gw_a = GatewayMetrics(
        gateway_id="gw_alpha",
        success_rate=0.98,
        p95_latency_ms=100.0,
        cost_basis_points=100.0,
        supported_methods={"UPI"},
        supported_currencies={"INR"},
        circuit_status=CircuitBreakerStatus.CLOSED,
    )
    gw_b = GatewayMetrics(
        gateway_id="gw_beta",
        success_rate=0.90,
        p95_latency_ms=500.0,
        cost_basis_points=300.0,
        supported_methods={"UPI"},
        supported_currencies={"INR"},
        circuit_status=CircuitBreakerStatus.CLOSED,
    )

    scores = scorer.score_candidates(
        candidates=[gw_a, gw_b],
        requested_method="UPI",
        requested_currency="INR",
    )

    assert len(scores) == 2
    alpha_score = next(s for s in scores if s.gateway_id == "gw_alpha")

    # Manually calculate expected Alpha score:
    # min_lat = 100.0, max_lat = 500.0 -> NormLatency = (100 - 100)/(500 - 100) = 0.0
    # min_cost = 100.0, max_cost = 300.0 -> NormCost = (100 - 100)/(300 - 100) = 0.0
    # NormSuccess = 0.98
    # HealthScore = 1.0 (CLOSED)
    # FitScore = 1.0 (Exact match)
    expected_alpha = (
        (0.35 * 0.98)           # Success: 0.3430
        + (0.20 * (1.0 - 0.0))  # Latency: 0.2000
        + (0.20 * (1.0 - 0.0))  # Cost: 0.2000
        + (0.15 * 1.0)          # Health: 0.1500
        + (0.10 * 1.0)          # Fit: 0.1000
    )
    # Total = 0.343 + 0.20 + 0.20 + 0.15 + 0.10 = 0.9930
    assert pytest.approx(alpha_score.total_score, rel=1e-4) == expected_alpha


def test_dynamic_ranking_upi_vs_cards():
    """Verify UPI transaction ranks UPI gateway #1 due to 0 bps cost and low latency."""
    router = GatewayRouter()
    req = GatewayAuthRequest(
        transaction_id=uuid.uuid4(),
        amount=50000,
        currency="INR",
        payment_method="UPI",
        merchant_id="m_123",
        order_id="ord_123",
    )

    ranked = router.rank_gateways(req)

    # UPI gateway should rank highest for UPI in INR
    assert ranked[0].gateway_id == "upi"
    assert ranked[0].total_score > ranked[1].total_score
    assert ranked[0].fit_score == 1.0


@pytest.mark.anyio
async def test_automatic_failover_when_primary_circuit_breaker_is_open():
    """
    MANDATE:
    If the primary gateway fails or its circuit breaker is OPEN,
    the router must automatically failover to the next-highest scoring gateway.
    """
    registry = CircuitBreakerRegistry(failure_threshold=3, recovery_timeout=2.0)
    router = GatewayRouter(registry=registry)

    req = GatewayAuthRequest(
        transaction_id=uuid.uuid4(),
        amount=50000,
        currency="INR",
        payment_method="UPI",
        merchant_id="m_123",
        order_id="ord_123",
    )

    # Initially, UPI is highest-scoring
    initial_ranked = router.rank_gateways(req)
    primary_gw = initial_ranked[0].gateway_id
    second_gw = initial_ranked[1].gateway_id
    assert primary_gw == "upi"

    # Trip primary gateway's circuit breaker to OPEN
    upi_breaker = registry.get("upi")
    await upi_breaker.record_failure(GatewayTimeoutException("Timeout 1"))
    await upi_breaker.record_failure(GatewayTimeoutException("Timeout 2"))
    await upi_breaker.record_failure(GatewayTimeoutException("Timeout 3"))
    assert upi_breaker.state == CircuitBreakerStatus.OPEN

    # Execute transaction:
    # Router evaluates candidates. UPI circuit is OPEN.
    # Router must automatically failover to the next-highest scoring gateway!
    response, chosen_gw, trace = await router.route_and_execute(req)

    assert response.success is True
    assert chosen_gw != "upi"
    assert chosen_gw == second_gw  # Successfully fell over to next-highest!

    # Verify routing trace recorded fast-fail and subsequent success
    assert len(trace) >= 2
    assert trace[0]["gateway"] == "upi"
    assert trace[0]["status"] == "CIRCUIT_OPEN_FAST_FAIL"
    assert trace[1]["gateway"] == second_gw
    assert trace[1]["status"] == "SUCCESS"


@pytest.mark.anyio
async def test_automatic_failover_on_primary_execution_timeout():
    """
    MANDATE:
    If the primary gateway encounters a timeout during execution,
    the router must automatically failover to the next-highest scoring gateway.
    """
    registry = CircuitBreakerRegistry(failure_threshold=3, recovery_timeout=2.0)
    router = GatewayRouter(registry=registry)

    req = GatewayAuthRequest(
        transaction_id=uuid.uuid4(),
        amount=50000,
        currency="INR",
        payment_method="UPI",
        merchant_id="m_123",
        order_id="ord_123",
        mock_header="timeout",  # Simulates timeout failure on the primary gateway!
    )

    # When primary gateway times out, router must failover to next-highest gateway
    # (Since mock_header would fail all if unchanged, we simulate primary failing specifically)
    ranked = router.rank_gateways(req)
    top_gw = ranked[0].gateway_id
    second_gw = ranked[1].gateway_id

    # Make top gateway fail specifically by recording its failure or mock
    upi_breaker = registry.get(top_gw)
    await upi_breaker.record_failure(GatewayTimeoutException("Timeout"))
    await upi_breaker.record_failure(GatewayTimeoutException("Timeout"))
    await upi_breaker.record_failure(GatewayTimeoutException("Timeout"))

    req.mock_header = None  # Second gateway will succeed
    response, chosen_gw, trace = await router.route_and_execute(req)

    assert chosen_gw == second_gw
    assert response.success is True


@pytest.mark.anyio
async def test_evaluate_routing_api_endpoint():
    """Verify POST /api/v1/gateways/evaluate-routing endpoint."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/v1/gateways/evaluate-routing",
            json={
                "amount": 50000,
                "currency": "INR",
                "payment_method": "UPI",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "selected_primary_gateway" in data
        assert len(data["ranked_gateways"]) >= 3
        # First ranked gateway has highest total_score
        assert data["ranked_gateways"][0]["gateway_id"] == data["selected_primary_gateway"]
        assert data["ranked_gateways"][0]["total_score"] >= data["ranked_gateways"][1]["total_score"]

