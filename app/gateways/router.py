import logging
from typing import Any

from app.core.enums import CircuitBreakerStatus
from app.gateways.base import GatewayAuthRequest, GatewayAuthResponse, PaymentGateway
from app.gateways.exceptions import (
    CircuitBreakerOpenException,
    GatewayDeclinedException,
    GatewayException,
    GatewayServiceUnavailableException,
    GatewayTimeoutException,
)
from app.gateways.payu import PayUGateway
from app.gateways.razorpay import RazorpayGateway
from app.gateways.stripe import StripeGateway
from app.gateways.upi import UPIGateway
from app.services.circuit_breaker import (
    CircuitBreakerRegistry,
    circuit_breaker_registry,
)
from app.services.scoring import (
    GatewayMetrics,
    MultiCriteriaScorer,
    ScoreBreakdown,
)

logger = logging.getLogger("payment_layer.gateway_router")


# Default Operational Benchmarks for Initialized Gateways
DEFAULT_GATEWAY_BENCHMARKS: dict[str, dict[str, Any]] = {
    "upi": {
        "success_rate": 0.98,
        "p95_latency_ms": 120.0,
        "cost_basis_points": 0.0,  # 0% MDR for UPI
        "supported_methods": {"UPI"},
        "supported_currencies": {"INR"},
    },
    "razorpay": {
        "success_rate": 0.96,
        "p95_latency_ms": 220.0,
        "cost_basis_points": 180.0,  # 1.8%
        "supported_methods": {"CARD", "UPI", "NETBANKING", "WALLET"},
        "supported_currencies": {"INR", "USD", "EUR"},
    },
    "stripe": {
        "success_rate": 0.99,
        "p95_latency_ms": 320.0,
        "cost_basis_points": 290.0,  # 2.9%
        "supported_methods": {"CARD", "WALLET"},
        "supported_currencies": {"USD", "EUR", "GBP", "INR", "SGD", "AED"},
    },
    "payu": {
        "success_rate": 0.95,
        "p95_latency_ms": 250.0,
        "cost_basis_points": 150.0,  # 1.5%
        "supported_methods": {"CARD", "NETBANKING", "UPI"},
        "supported_currencies": {"INR"},
    },
}


class GatewayRouter:
    """
    Intelligent Payment Gateway Router driven by Multi-Criteria Scoring & Circuit Breakers.

    Formula:
    Score = (W_success * NormSuccess)
          + (W_latency * (1 - NormLatency))
          + (W_cost * (1 - NormCost))
          + (W_health * HealthScore)
          + (W_fit * FitScore)

    Automatic Failover Behavior:
    - Calculates dynamic scores for all candidate gateways.
    - Evaluates in descending order of dynamic multi-criteria score.
    - If the primary gateway fails or its circuit breaker is OPEN, the router
      automatically fails over to the next-highest scoring gateway.
    """

    def __init__(
        self,
        registry: CircuitBreakerRegistry | None = None,
        gateways: dict[str, PaymentGateway] | None = None,
        scorer: MultiCriteriaScorer | None = None,
    ):
        self.circuit_breakers = registry or circuit_breaker_registry
        self.gateways: dict[str, PaymentGateway] = gateways or {
            "razorpay": RazorpayGateway(),
            "stripe": StripeGateway(),
            "payu": PayUGateway(),
            "upi": UPIGateway(),
        }
        self.scorer = scorer or MultiCriteriaScorer()
        self._metrics: dict[str, GatewayMetrics] = {}
        self._init_default_metrics()

    def _init_default_metrics(self) -> None:
        """Initializes default telemetry benchmarks for registered gateways."""
        for gid, bench in DEFAULT_GATEWAY_BENCHMARKS.items():
            self._metrics[gid] = GatewayMetrics(
                gateway_id=gid,
                success_rate=bench["success_rate"],
                p95_latency_ms=bench["p95_latency_ms"],
                cost_basis_points=bench["cost_basis_points"],
                supported_methods=bench["supported_methods"],
                supported_currencies=bench["supported_currencies"],
            )

    def register_gateway(
        self,
        gateway: PaymentGateway,
        metrics: GatewayMetrics | None = None,
    ) -> None:
        """Registers a payment gateway and its operational metrics."""
        gid = gateway.gateway_id.lower().strip()
        self.gateways[gid] = gateway
        if metrics:
            self._metrics[gid] = metrics
        elif gid not in self._metrics:
            self._metrics[gid] = GatewayMetrics(
                gateway_id=gid,
                success_rate=0.95,
                p95_latency_ms=250.0,
                cost_basis_points=200.0,
                supported_methods={"CARD"},
                supported_currencies={"USD", "INR"},
            )

    def set_gateway_metrics(self, gateway_id: str, metrics: GatewayMetrics) -> None:
        """Sets custom or updated metrics for a gateway."""
        self._metrics[gateway_id.lower().strip()] = metrics

    def get_gateway_metrics(self, gateway_id: str) -> GatewayMetrics | None:
        """Retrieves current metrics for a gateway."""
        return self._metrics.get(gateway_id.lower().strip())

    def get_circuit_breaker_status(self, gateway_id: str) -> CircuitBreakerStatus:
        """Returns the current Circuit Breaker status for a gateway."""
        return self.circuit_breakers.get(gateway_id).state

    def get_gateway(self, gateway_id: str) -> PaymentGateway | None:
        """Returns the gateway adapter instance for the specified identifier."""
        return self.gateways.get(gateway_id.lower().strip())

    def rank_gateways(
        self,
        request: GatewayAuthRequest,
        candidate_ids: list[str] | None = None,
    ) -> list[ScoreBreakdown]:
        """
        Dynamically calculates multi-criteria scores for candidate gateways and returns
        them ranked in descending order (highest score first).
        """
        target_ids = [gid.lower().strip() for gid in (candidate_ids or list(self.gateways.keys()))]
        candidates: list[GatewayMetrics] = []

        for gid in target_ids:
            if gid not in self.gateways:
                continue

            base_metrics = self._metrics.get(gid)
            if not base_metrics:
                base_metrics = GatewayMetrics(
                    gateway_id=gid,
                    success_rate=0.95,
                    p95_latency_ms=250.0,
                    cost_basis_points=200.0,
                )

            # Synchronize with live circuit breaker state
            cb = self.circuit_breakers.get(gid)
            live_metrics = GatewayMetrics(
                gateway_id=gid,
                success_rate=base_metrics.success_rate,
                p95_latency_ms=base_metrics.p95_latency_ms,
                cost_basis_points=base_metrics.cost_basis_points,
                supported_methods=base_metrics.supported_methods,
                supported_currencies=base_metrics.supported_currencies,
                circuit_status=cb.state,
                consecutive_failures=cb.consecutive_failures,
            )
            candidates.append(live_metrics)

        return self.scorer.score_candidates(
            candidates=candidates,
            requested_method=request.payment_method,
            requested_currency=request.currency,
        )

    async def route_and_execute(
        self,
        request: GatewayAuthRequest,
        candidate_ids: list[str] | None = None,
    ) -> tuple[GatewayAuthResponse, str, list[dict[str, Any]]]:
        """
        Core multi-criteria dynamic routing algorithm with failover:
        1. Ranks all candidates using the multi-criteria formula.
        2. Tries the highest-scoring gateway (Primary).
        3. If the primary fails or its circuit breaker is OPEN, automatically
           fails over to the next-highest scoring gateway.
        """
        ranked_scores = self.rank_gateways(request, candidate_ids)
        if not ranked_scores:
            raise GatewayException(
                message=f"No viable payment gateways configured for transaction {request.transaction_id}",
                error_code="NO_CANDIDATE_GATEWAYS",
            )

        logger.info(
            "Transaction %s candidate rankings: %s",
            request.transaction_id,
            [(s.gateway_id, round(s.total_score, 4)) for s in ranked_scores],
        )

        routing_trace: list[dict[str, Any]] = []
        last_exception: Exception | None = None

        for ranking in ranked_scores:
            gw_id = ranking.gateway_id
            gateway = self.gateways[gw_id]
            breaker = self.circuit_breakers.get(gw_id)

            trace_entry = {
                "gateway": gw_id,
                "score": round(ranking.total_score, 4),
                "score_breakdown": ranking.to_dict(),
                "circuit_state": breaker.state.value,
            }

            try:
                logger.info(
                    "Attempting gateway '%s' (Score: %.4f, Circuit: %s) for txn %s",
                    gw_id,
                    ranking.total_score,
                    breaker.state.value,
                    request.transaction_id,
                )

                # Execute through Circuit Breaker (fails fast if OPEN)
                response = await breaker.call(gateway.authorize, request)

                trace_entry["status"] = "SUCCESS"
                routing_trace.append(trace_entry)
                return response, gw_id, routing_trace

            except CircuitBreakerOpenException as exc:
                # Primary circuit breaker is OPEN: fast-fail (<1ms) -> failover to next-highest scoring!
                logger.warning(
                    "Gateway '%s' circuit is OPEN. Failing over to next-highest scoring gateway: %s",
                    gw_id,
                    str(exc),
                )
                trace_entry["status"] = "CIRCUIT_OPEN_FAST_FAIL"
                trace_entry["reason"] = str(exc)
                routing_trace.append(trace_entry)
                last_exception = exc
                continue

            except (GatewayTimeoutException, GatewayServiceUnavailableException) as exc:
                # Primary gateway failed with timeout or 5xx: failover to next-highest scoring!
                logger.warning(
                    "Gateway '%s' execution failed (%s). Failing over to next-highest scoring gateway.",
                    gw_id,
                    type(exc).__name__,
                )
                trace_entry["status"] = "GATEWAY_FAILED"
                trace_entry["reason"] = str(exc)
                routing_trace.append(trace_entry)
                last_exception = exc
                continue

            except GatewayDeclinedException as exc:
                # Business decline from card issuer (insufficient funds, wrong PIN)
                # Does NOT trigger route failover
                trace_entry["status"] = "DECLINED"
                trace_entry["reason"] = str(exc)
                routing_trace.append(trace_entry)
                raise exc

        if last_exception:
            raise last_exception

        raise GatewayException(
            message=f"All candidate gateways failed for transaction {request.transaction_id}. "
            f"Routing trace: {routing_trace}",
            error_code="ALL_GATEWAYS_FAILED",
        )

    async def execute_with_failover(
        self,
        request: GatewayAuthRequest,
        priority_gateways: list[str] | None = None,
    ) -> tuple[GatewayAuthResponse, str, list[dict[str, Any]]]:
        """
        Executes payment authorization with failover.
        If priority_gateways is explicitly specified, evaluates in that explicit priority order.
        Otherwise, uses dynamic multi-criteria scoring via route_and_execute.
        """
        if not priority_gateways:
            return await self.route_and_execute(request=request)

        routing_trace: list[dict[str, Any]] = []
        last_exception: Exception | None = None

        for gw_name in priority_gateways:
            gw_id = gw_name.lower().strip()
            gateway = self.gateways.get(gw_id)
            if not gateway:
                continue

            breaker = self.circuit_breakers.get(gw_id)
            trace_entry = {
                "gateway": gw_id,
                "circuit_state": breaker.state.value,
            }

            try:
                response = await breaker.call(gateway.authorize, request)
                trace_entry["status"] = "SUCCESS"
                routing_trace.append(trace_entry)
                return response, gw_id, routing_trace
            except CircuitBreakerOpenException as exc:
                trace_entry["status"] = "CIRCUIT_OPEN_FAST_FAIL"
                trace_entry["reason"] = str(exc)
                routing_trace.append(trace_entry)
                last_exception = exc
                continue
            except (GatewayTimeoutException, GatewayServiceUnavailableException) as exc:
                trace_entry["status"] = "GATEWAY_FAILED"
                trace_entry["reason"] = str(exc)
                routing_trace.append(trace_entry)
                last_exception = exc
                continue
            except GatewayDeclinedException as exc:
                trace_entry["status"] = "DECLINED"
                trace_entry["reason"] = str(exc)
                routing_trace.append(trace_entry)
                raise exc

        if last_exception:
            raise last_exception

        raise GatewayException(
            message=f"All candidate gateways failed for transaction {request.transaction_id}. "
            f"Routing trace: {routing_trace}",
            error_code="ALL_GATEWAYS_FAILED",
        )


# Aliases for architectural clarity
ResilientGatewayRouter = GatewayRouter

# Global singleton router
gateway_router = GatewayRouter()
