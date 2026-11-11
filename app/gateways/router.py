import logging
from typing import Dict, List, Optional, Tuple

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
from app.services.circuit_breaker import CircuitBreakerRegistry, circuit_breaker_registry

logger = logging.getLogger("payment_layer.gateway_router")


class ResilientGatewayRouter:
    """
    High-availability Payment Gateway Router integrating Circuit Breakers.
    
    Architectural Behavior:
    - Evaluates gateways in configured priority order.
    - Wraps every gateway invocation in its dedicated CircuitBreaker.
    - If a primary gateway repeatedly times out, it trips to OPEN.
    - In OPEN state, the circuit breaker FAILS-FAST (<1ms), instantly routing traffic
      to the next alternate gateway.
    - Within 2 seconds, the tripped gateway transitions to HALF_OPEN, allowing canary probes.
    """

    def __init__(
        self,
        registry: Optional[CircuitBreakerRegistry] = None,
        gateways: Optional[Dict[str, PaymentGateway]] = None,
    ):
        self.circuit_breakers = registry or circuit_breaker_registry
        self.gateways: Dict[str, PaymentGateway] = gateways or {
            "razorpay": RazorpayGateway(),
            "stripe": StripeGateway(),
            "payu": PayUGateway(),
            "upi": UPIGateway(),
        }

    def register_gateway(self, gateway: PaymentGateway) -> None:
        """Registers a custom payment gateway adapter."""
        self.gateways[gateway.gateway_id.lower().strip()] = gateway

    def get_circuit_breaker_status(self, gateway_id: str) -> CircuitBreakerStatus:
        """Returns the current Circuit Breaker status for a gateway."""
        return self.circuit_breakers.get(gateway_id).state

    async def execute_with_failover(
        self,
        request: GatewayAuthRequest,
        priority_gateways: Optional[List[str]] = None,
    ) -> Tuple[GatewayAuthResponse, str, List[Dict[str, str]]]:
        """
        Executes payment authorization with automatic circuit-breaker failover:
        - Tries gateways in order of priority.
        - If a gateway's circuit breaker is OPEN, it fails-fast immediately and attempts the next alternate.
        - If a gateway encounters a timeout or 5xx, it records the failure on the circuit breaker and falls over.
        - Returns: (successful_response, successful_gateway_id, routing_trace)
        """
        candidate_gateways = priority_gateways or ["razorpay", "stripe", "payu", "upi"]
        routing_trace: List[Dict[str, str]] = []

        last_exception: Optional[Exception] = None

        for gw_name in candidate_gateways:
            gw_id = gw_name.lower().strip()
            gateway = self.gateways.get(gw_id)
            if not gateway:
                logger.warning("Gateway '%s' requested in route but not registered", gw_id)
                continue

            breaker = self.circuit_breakers.get(gw_id)

            try:
                logger.info(
                    "Routing transaction %s to gateway '%s' (circuit state: %s)",
                    request.transaction_id,
                    gw_id,
                    breaker.state.value,
                )

                # Execute through Circuit Breaker (fails fast if OPEN)
                response = await breaker.call(gateway.authorize, request)

                routing_trace.append({
                    "gateway": gw_id,
                    "status": "SUCCESS",
                    "circuit_state": breaker.state.value,
                })
                return response, gw_id, routing_trace

            except CircuitBreakerOpenException as exc:
                # FAST FAIL (<1ms): Circuit is OPEN. Immediately fall over to next alternate gateway!
                logger.warning(
                    "Gateway '%s' circuit is OPEN (fails-fast). Immediate failover initiated: %s",
                    gw_id,
                    str(exc),
                )
                routing_trace.append({
                    "gateway": gw_id,
                    "status": "CIRCUIT_OPEN_FAST_FAIL",
                    "reason": str(exc),
                })
                last_exception = exc
                continue

            except (GatewayTimeoutException, GatewayServiceUnavailableException) as exc:
                # Gateway timed out or was unavailable. Circuit breaker recorded failure. Fall over!
                logger.warning(
                    "Gateway '%s' failed with error (%s). Falling over to next gateway.",
                    gw_id,
                    type(exc).__name__,
                )
                routing_trace.append({
                    "gateway": gw_id,
                    "status": "GATEWAY_FAILED",
                    "reason": str(exc),
                })
                last_exception = exc
                continue

            except GatewayDeclinedException as exc:
                # Business decline from issuing bank (insufficient funds, wrong PIN)
                # Does NOT trip the circuit breaker; declines are terminal business decisions
                routing_trace.append({
                    "gateway": gw_id,
                    "status": "DECLINED",
                    "reason": str(exc),
                })
                raise exc

        # If all candidates exhausted
        raise GatewayException(
            message=f"All candidate gateways failed for transaction {request.transaction_id}. "
            f"Routing trace: {routing_trace}",
            error_code="ALL_GATEWAYS_FAILED",
        ) from last_exception


# Global singleton router
gateway_router = ResilientGatewayRouter()
