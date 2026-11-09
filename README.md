# Payment Orchestration Layer

An enterprise-grade, mission-critical Payment Orchestration Layer built with **FastAPI**, **PostgreSQL**, **SQLAlchemy 2.0 (Async)**, and **Alembic**.

Designed by following strict financial engineering standards (zero float tolerances, immutable audit logs, composite key deduplication, and distributed idempotency locking).

---

## 🏛️ Architectural Mandates & Invariants

### 1. Zero Floating-Point Precision Rule
> **MANDATE**: ALL financial amounts must be represented as **`BIGINT`** storing the smallest minor currency unit (e.g. **paise** for INR, **cents** for USD/EUR, **pence** for GBP). **FLOATS ARE STRICTLY PROHIBITED** across the entire stack—database columns, SQLAlchemy models, and Pydantic validation schemas.
- Example: ₹500.25 is stored strictly as `50025` (BIGINT).
- Eliminates IEEE 754 binary floating-point rounding errors and fractional penny drift.

### 2. Immutable State Transition Audit Trail
> **MANDATE**: The `transaction_state_log` is an append-only ledger.
- Zero in-place updates: rows are never modified once written (`updated_at` column is forbidden).
- Every state shift (`INITIATED` -> `PENDING` -> `AUTHORIZED` -> `CAPTURED` -> `SETTLED` / `FAILED`) captures the triggering actor, source event (API, Webhook, Sweeper, Reconciler), and raw sanitized payload.

### 3. Asynchronous Webhook Deduplication Engine
> **MANDATE**: `processed_webhook_events` strictly uses a **Composite Primary Key** of `(gateway, event_id)`.
- Eliminates duplicate processing of webhook retries from upstream PSPs (e.g., Stripe, Razorpay, Adyen).
- Guarantees exactly-once asynchronous state reconciliation.

### 4. Distributed Idempotency Engine
> **MANDATE**: `idempotency_keys` enforces distributed lease locking on incoming transaction requests.
- Caches HTTP status codes and responses for repeat submissions with identical payloads.
- Rejects concurrent double-clicks or collisions with `409 Conflict` (`Retry-After`).
- Detects payload mismatch tampering on reused keys with `422 Unprocessable Entity`.

---

## 📊 Database Schema (All 8 Core Tables)

```mermaid
erDiagram
    GATEWAY_CONFIG ||--o{ GATEWAY_ROUTES : "configured for"
    GATEWAY_CONFIG ||--o{ GATEWAY_HEALTH_METRICS : "emits telemetry"
    GATEWAY_CONFIG ||--o{ TRANSACTIONS : "routes to"
    ROUTING_CONFIG ||--o{ GATEWAY_ROUTES : "contains rules"
    TRANSACTIONS ||--|{ TRANSACTION_STATE_LOG : "generates immutable history"
    
    GATEWAY_CONFIG {
        string id PK "stripe_primary, razorpay_in"
        string name
        string gateway_type
        string status "ACTIVE, DRAINING, INACTIVE"
        string api_base_url
        string credentials_vault_ref "KMS/Vault secret ARN"
        jsonb supported_currencies
        jsonb supported_payment_methods
        int rate_limit_tps
        int timeout_ms
        timestamptz created_at
        timestamptz updated_at
    }

    ROUTING_CONFIG {
        uuid id PK
        string merchant_id "Nullable for platform default"
        string name
        bool is_active
        string algorithm "PRIORITY, WEIGHTED, LEAST_LATENCY"
        bool failover_enabled
        int max_retry_attempts
        int circuit_breaker_error_threshold_pct
        timestamptz created_at
        timestamptz updated_at
    }

    GATEWAY_ROUTES {
        uuid id PK
        uuid routing_config_id FK
        string gateway_id FK
        int priority "Lower = Higher precedence"
        int weight "Traffic split (0-100)"
        string currency "ISO 4217 or NULL (any)"
        string payment_method "CARD, UPI, etc."
        string card_network "VISA, MASTERCARD, RUPAY"
        bigint min_amount "BIGINT paise/cents"
        bigint max_amount "BIGINT paise/cents"
        bool is_active
        timestamptz created_at
        timestamptz updated_at
    }

    TRANSACTIONS {
        uuid id PK
        string merchant_id
        string customer_id
        string order_id
        bigint amount "BIGINT paise/cents (NEVER float)"
        string currency "ISO 4217 (INR, USD, etc.)"
        bigint fee_amount "BIGINT paise/cents"
        bigint tax_amount "BIGINT paise/cents"
        bigint refunded_amount "BIGINT paise/cents"
        string status
        string payment_method
        jsonb payment_method_details
        string gateway_id FK
        string gateway_transaction_id
        string gateway_reference_id
        string error_code
        text error_message
        jsonb metadata
        timestamptz created_at
        timestamptz updated_at
    }

    TRANSACTION_STATE_LOG {
        bigint id PK "Monotonic 64-bit BigSerial"
        uuid transaction_id FK
        string from_state "NULL on init"
        string to_state
        string trigger "API_REQUEST, WEBHOOK, RECON"
        string actor_id
        jsonb gateway_payload
        jsonb transition_metadata
        timestamptz created_at "Immutable - No updated_at"
    }

    IDEMPOTENCY_KEYS {
        uuid id PK
        string key "UK: (key, merchant_id)"
        string merchant_id "UK: (key, merchant_id)"
        string request_path
        string request_hash "SHA-256"
        string status "PENDING, COMPLETED, FAILED"
        int response_code
        jsonb response_headers
        jsonb response_body
        timestamptz locked_until
        timestamptz expires_at
        timestamptz created_at
        timestamptz updated_at
    }

    PROCESSED_WEBHOOK_EVENTS {
        string gateway PK "Composite PK (gateway, event_id)"
        string event_id PK "Composite PK (gateway, event_id)"
        string event_type
        string payload_hash "SHA-256"
        string status "RECEIVED, PROCESSED, IGNORED"
        jsonb raw_payload
        text processing_error
        timestamptz received_at
        timestamptz processed_at
    }

    GATEWAY_HEALTH_METRICS {
        uuid id PK
        string gateway_id FK
        timestamptz window_start
        timestamptz window_end
        int total_requests
        int successful_requests
        int failed_requests
        int p50_latency_ms
        int p95_latency_ms
        int p99_latency_ms
        string circuit_breaker_status "CLOSED, HALF_OPEN, OPEN"
        timestamptz last_heartbeat_at
        timestamptz created_at
    }
```

---

## 🚀 Quickstart & Setup

### 1. Requirements
- Python 3.11+
- PostgreSQL 14+ (or compatible DB)

### 2. Install Dependencies
```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Database Migrations (Alembic)

To generate offline static SQL for verification or production DBAs:
```bash
alembic upgrade head --sql
```

To apply migrations online to the target PostgreSQL database:
```bash
alembic upgrade head
```

To roll back migration:
```bash
alembic downgrade -1
```

### 4. Running the Application
```bash
uvicorn app.main:app --reload --port 8000
```
- Interactive Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
- Interactive ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)
- Health Check: [http://localhost:8000/health](http://localhost:8000/health)

### 5. Running Tests
```bash
pytest -v
```

---

## 🔒 Financial Precision Verification

Our test suite rigorously verifies that:
1. Every monetary column across `transactions` and `gateway_routes` is an instance of `sqlalchemy.BigInteger`.
2. Zero `Float` or `Double` precision types exist in any financial column.
3. `processed_webhook_events` is anchored by the composite primary key `(gateway, event_id)`.
4. `transaction_state_log` has no `updated_at` column, guaranteeing immutable audit trails.
5. Pydantic request models fail validation if a float amount is submitted (e.g. `500.50`), requiring exact integer minor units (e.g. `50050` paise).
