from enum import Enum


class TransactionState(str, Enum):
    """
    Deterministic 12-state transaction life-cycle states.
    All transitions must adhere to the deterministic state machine graph.
    """
    CREATED = "CREATED"
    ROUTE_SELECTED = "ROUTE_SELECTED"
    AUTH_INITIATED = "AUTH_INITIATED"
    AUTHORISED = "AUTHORISED"
    AUTH_FAILED = "AUTH_FAILED"
    CAPTURE_INITIATED = "CAPTURE_INITIATED"
    CAPTURED = "CAPTURED"
    PARTIALLY_CAPTURED = "PARTIALLY_CAPTURED"
    CAPTURE_FAILED = "CAPTURE_FAILED"
    REFUND_INITIATED = "REFUND_INITIATED"
    REFUNDED = "REFUNDED"
    FAILED = "FAILED"

    # Alias for spelling interoperability
    AUTHORIZED = "AUTHORISED"


# Canonical alias for compatibility
TransactionStatus = TransactionState


class PaymentMethod(str, Enum):
    CARD = "CARD"
    UPI = "UPI"
    NETBANKING = "NETBANKING"
    WALLET = "WALLET"
    BNPL = "BNPL"
    NACH = "NACH"


class Currency(str, Enum):
    INR = "INR"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    SGD = "SGD"
    AED = "AED"


class StateTrigger(str, Enum):
    API_REQUEST = "API_REQUEST"
    ROUTE_SELECTED = "ROUTE_SELECTED"
    GATEWAY_CALL = "GATEWAY_CALL"
    GATEWAY_WEBHOOK = "GATEWAY_WEBHOOK"
    GATEWAY_RESPONSE = "GATEWAY_RESPONSE"
    GATEWAY_TIMEOUT = "GATEWAY_TIMEOUT"
    RECONCILIATION_JOB = "RECONCILIATION_JOB"
    TIMEOUT_SWEEPER = "TIMEOUT_SWEEPER"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    CLIENT_CANCEL = "CLIENT_CANCEL"


class GatewayType(str, Enum):
    STRIPE = "STRIPE"
    RAZORPAY = "RAZORPAY"
    ADYEN = "ADYEN"
    JUSPAY = "JUSPAY"
    PAYPAL = "PAYPAL"
    CASHFREE = "CASHFREE"


class GatewayStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    INACTIVE = "INACTIVE"
    MAINTENANCE = "MAINTENANCE"


class CircuitBreakerStatus(str, Enum):
    CLOSED = "CLOSED"
    HALF_OPEN = "HALF_OPEN"
    OPEN = "OPEN"


class RoutingAlgorithm(str, Enum):
    PRIORITY = "PRIORITY"
    WEIGHTED = "WEIGHTED"
    LEAST_LATENCY = "LEAST_LATENCY"
    LOWEST_COST = "LOWEST_COST"
    SMART_DYNAMIC = "SMART_DYNAMIC"


class WebhookProcessingStatus(str, Enum):
    RECEIVED = "RECEIVED"
    PROCESSED = "PROCESSED"
    IGNORED = "IGNORED"
    FAILED = "FAILED"


class IdempotencyStatus(str, Enum):
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    # Alias for backwards compatibility
    PENDING = "PROCESSING"
