from enum import Enum


class TransactionStatus(str, Enum):
    INITIATED = "INITIATED"
    PENDING = "PENDING"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    SETTLED = "SETTLED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"


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
    GATEWAY_WEBHOOK = "GATEWAY_WEBHOOK"
    RECONCILIATION_JOB = "RECONCILIATION_JOB"
    TIMEOUT_SWEEPER = "TIMEOUT_SWEEPER"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"


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
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

