from dataclasses import dataclass, field
from typing import Any

from app.core.enums import CircuitBreakerStatus

# =============================================================================
# MULTI-CRITERIA SCORING WEIGHTS (SUM = 1.00)
# =============================================================================
WEIGHT_SUCCESS = 0.35  # 35% - Success Rate
WEIGHT_LATENCY = 0.20  # 20% - P95 Latency
WEIGHT_COST = 0.20  # 20% - Cost Efficiency
WEIGHT_HEALTH = 0.15  # 15% - Gateway Health & Circuit Breaker State
WEIGHT_FIT = 0.10  # 10% - Payment Method & Currency Compatibility


@dataclass
class GatewayMetrics:
    """Telemetry and operational metrics for a payment gateway adapter."""

    gateway_id: str
    success_rate: float  # e.g. 0.98 for 98%
    p95_latency_ms: float  # e.g. 150.0 ms
    cost_basis_points: float  # e.g. 180.0 bps (1.8%) or 0 for UPI
    supported_methods: set[str] = field(default_factory=set)
    supported_currencies: set[str] = field(default_factory=set)
    circuit_status: CircuitBreakerStatus = CircuitBreakerStatus.CLOSED
    consecutive_failures: int = 0


@dataclass
class ScoreBreakdown:
    """Detailed score breakdown calculated for a gateway candidate."""

    gateway_id: str
    total_score: float
    norm_success: float
    norm_latency: float
    norm_cost: float
    health_score: float
    fit_score: float
    circuit_status: CircuitBreakerStatus
    raw_metrics: GatewayMetrics | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gateway_id": self.gateway_id,
            "total_score": round(self.total_score, 4),
            "norm_success": round(self.norm_success, 4),
            "norm_latency": round(self.norm_latency, 4),
            "norm_cost": round(self.norm_cost, 4),
            "health_score": round(self.health_score, 4),
            "fit_score": round(self.fit_score, 4),
            "circuit_status": self.circuit_status.value,
        }


class MultiCriteriaScorer:
    """
    Evaluates and ranks payment gateways using the exact architectural formula:

    Score = (W_success * NormSuccess)
          + (W_latency * (1 - NormLatency))
          + (W_cost * (1 - NormCost))
          + (W_health * HealthScore)
          + (W_fit * FitScore)
    """

    def __init__(
        self,
        w_success: float = WEIGHT_SUCCESS,
        w_latency: float = WEIGHT_LATENCY,
        w_cost: float = WEIGHT_COST,
        w_health: float = WEIGHT_HEALTH,
        w_fit: float = WEIGHT_FIT,
        max_latency_ceiling_ms: float = 2000.0,
        max_cost_ceiling_bps: float = 400.0,
    ):
        self.w_success = w_success
        self.w_latency = w_latency
        self.w_cost = w_cost
        self.w_health = w_health
        self.w_fit = w_fit
        self.max_latency_ceiling_ms = max_latency_ceiling_ms
        self.max_cost_ceiling_bps = max_cost_ceiling_bps

    def compute_health_score(
        self,
        circuit_status: CircuitBreakerStatus,
        consecutive_failures: int = 0,
    ) -> float:
        """
        Calculates HealthScore based on Circuit Breaker state:
        - CLOSED: 1.0 (with minor penalty for transient consecutive failures)
        - HALF_OPEN: 0.5 (canary trial probe)
        - OPEN: 0.0 (tripped / offline)
        """
        if circuit_status == CircuitBreakerStatus.OPEN:
            return 0.0
        elif circuit_status == CircuitBreakerStatus.HALF_OPEN:
            return 0.5
        else:
            # CLOSED: starts at 1.0, slight penalty per failure before threshold
            penalty = min(0.3, consecutive_failures * 0.1)
            return max(0.7, 1.0 - penalty)

    def compute_fit_score(
        self,
        gateway: GatewayMetrics,
        requested_method: str,
        requested_currency: str,
    ) -> float:
        """
        Calculates FitScore based on payment method and currency alignment:
        - Full match on method & currency: 1.0
        - Partial match / general card fallback: 0.5
        - Unsupported method or currency: 0.0
        """
        method_upper = requested_method.upper().strip()
        currency_upper = requested_currency.upper().strip()

        supported_methods_upper = {m.upper().strip() for m in gateway.supported_methods}
        supported_currencies_upper = {c.upper().strip() for c in gateway.supported_currencies}

        # If gateway doesn't support currency at all, fit is 0.0
        if supported_currencies_upper and currency_upper not in supported_currencies_upper:
            return 0.0

        if not supported_methods_upper or method_upper in supported_methods_upper:
            return 1.0

        # General card fallback or aggregator route
        if "CARD" in method_upper and any("CARD" in m for m in supported_methods_upper):
            return 0.8

        return 0.0

    def score_candidates(
        self,
        candidates: list[GatewayMetrics],
        requested_method: str,
        requested_currency: str,
    ) -> list[ScoreBreakdown]:
        """
        Evaluates a list of candidate gateways and returns their score breakdowns
        sorted descending by total_score.
        """
        if not candidates:
            return []

        # Relative min-max normalization across candidate pool
        latencies = [c.p95_latency_ms for c in candidates]
        costs = [c.cost_basis_points for c in candidates]

        min_lat, max_lat = min(latencies), max(latencies)
        min_cost, max_cost = min(costs), max(costs)

        scores: list[ScoreBreakdown] = []

        for c in candidates:
            # 1. NormSuccess [0.0, 1.0]
            norm_success = min(1.0, max(0.0, c.success_rate))

            # 2. NormLatency [0.0, 1.0]
            if max_lat > min_lat:
                norm_latency = (c.p95_latency_ms - min_lat) / (max_lat - min_lat)
            else:
                norm_latency = min(1.0, max(0.0, c.p95_latency_ms / self.max_latency_ceiling_ms))

            # 3. NormCost [0.0, 1.0]
            if max_cost > min_cost:
                norm_cost = (c.cost_basis_points - min_cost) / (max_cost - min_cost)
            else:
                norm_cost = min(1.0, max(0.0, c.cost_basis_points / self.max_cost_ceiling_bps))

            # 4. HealthScore [0.0, 1.0]
            health_score = self.compute_health_score(c.circuit_status, c.consecutive_failures)

            # 5. FitScore [0.0, 1.0]
            fit_score = self.compute_fit_score(c, requested_method, requested_currency)

            # 6. EXACT ARCHITECTURAL FORMULA
            # Score = (W_success * NormSuccess)
            #       + (W_latency * (1 - NormLatency))
            #       + (W_cost * (1 - NormCost))
            #       + (W_health * HealthScore)
            #       + (W_fit * FitScore)
            total_score = (
                (self.w_success * norm_success)
                + (self.w_latency * (1.0 - norm_latency))
                + (self.w_cost * (1.0 - norm_cost))
                + (self.w_health * health_score)
                + (self.w_fit * fit_score)
            )

            scores.append(
                ScoreBreakdown(
                    gateway_id=c.gateway_id,
                    total_score=total_score,
                    norm_success=norm_success,
                    norm_latency=norm_latency,
                    norm_cost=norm_cost,
                    health_score=health_score,
                    fit_score=fit_score,
                    circuit_status=c.circuit_status,
                    raw_metrics=c,
                )
            )

        # Sort descending: highest-scoring gateway first
        scores.sort(key=lambda s: s.total_score, reverse=True)
        return scores
