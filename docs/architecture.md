# ZeTheta Payment Orchestration Layer — Architecture Specification

## 1. System Overview & Architectural Philosophy

The **ZeTheta Payment Orchestration Layer** is an enterprise-grade, high-throughput payment switching and processing platform designed to orchestrate transactions across multiple Payment Service Providers (PSPs) including **Razorpay, Stripe, PayU, and UPI**.

The system is built on zero-trust financial principles:
1. **Zero Floating-Point Representation**: All monetary values are strictly represented as signed 64-bit integers (`BIGINT`), recording currency minor units (e.g., paise in INR, cents in USD, pence in GBP). Floating-point math is strictly forbidden at the API schema, business logic, and database persistence layers.
2. **Deterministic State Invariants**: All payments follow an immutable, directed acyclic state machine across 12 discrete states. Illegal transitions trigger immediate exceptions.
3. **Pessimistic Locking with I/O Decoupling**: Database row locks (`SELECT ... FOR UPDATE`) are held only during state transitions and are explicitly released before making long-running external HTTP API calls to payment gateways.
4. **Advisory Distributed Locking for Idempotency**: Concurrency collisions on the same idempotency key are serialized via PostgreSQL transaction advisory locks (`pg_advisory_xact_lock`), completely preventing double-charges.
5. **Failover Resilience**: Multi-criteria dynamic scoring routes transactions to the optimal gateway, backed by circuit breakers that trip in real time on gateway latency or degradation.
6. **Defensive Webhook Processing**: Constant-time HMAC-SHA256 signature verification (`timingSafeEqual`), composite primary key deduplication `(gateway, event_id)`, and Dead Letter Queue (DLQ) containment after 3 failed attempts.
7. **Automated Reconciliation**: Periodic background sweeper detecting transactions stalled in transient states for > 5 minutes, auto-reconciling against gateway ground-truth and generating `RECONCILIATION_OVERRIDE` audit logs.

```
                           +---------------------------+
                           |   Client / API Consumer   |
                           +-------------+-------------+
                                         |
                                  HTTPS (REST API)
                                         |
                                         v
                           +---------------------------+
                           |  FastAPI ASGI Application |
                           |  (Pydantic v2 Validation) |
                           +-------------+-------------+
                                         |
                     +-------------------+-------------------+
                     |                                       |
                     v                                       v
         +-----------------------+               +-----------------------+
         |  Idempotency Service  |               |  Transaction State    |
         |  (pg_advisory_lock)   |               |        Machine        |
         +-----------+-----------+               +-----------+-----------+
                     |                                       |
                     +-------------------+-------------------+
                                         |
                                         v
                           +---------------------------+
                           |  Smart Gateway Router     |
                           |  (Multi-Criteria Scoring) |
                           +-------------+-------------+
                                         |
                    +--------------------+--------------------+
                    |                    |                    |
                    v                    v                    v
           +-----------------+  +-----------------+  +-----------------+
           | Razorpay Adapter|  | Stripe Adapter  |  | PayU / UPI Adap |
           | Circuit Breaker |  | Circuit Breaker |  | Circuit Breaker |
           +--------+--------+  +--------+--------+  +--------+--------+
                    |                    |                    |
                    +--------------------+--------------------+
                                         |
                                         v
                           +---------------------------+
                           |    PostgreSQL 16 Engine   |
                           |    (Row & Advisory Locks) |
                           +---------------------------+
```

---

## 2. Technology Stack & Key Design Choices

### 2.1 FastAPI (Asynchronous Web Framework)
* **High-Concurrency ASGI**: Utilizes Python 3.13's `asyncio` event loop and `uvicorn` to process non-blocking I/O across thousands of simultaneous payment requests.
* **Declarative Schema Contracts**: Pydantic v2 models validate incoming payloads, stripping malicious fields, rejecting floating-point values, and enforcing UUID standards.
* **Uniform Error Envelope**: Centralized exception handlers catch all operational, domain, and gateway exceptions, formatting them into standardized JSON error responses with `error.code`, `error.message`, and `details.gateway_error_code`.

### 2.2 PostgreSQL 16 & SQLAlchemy 2.0 (Relational Storage)
* **ACID Transactions**: Financial ledgers require strict atomicity, consistency, isolation, and durability.
* **Row-Level Pessimistic Locking (`SELECT FOR UPDATE`)**: Guarantees deterministic state transitions and prevents concurrent race conditions on transaction records.
* **PostgreSQL Advisory Locks (`pg_advisory_xact_lock`)**: High-performance transaction-scoped distributed locking using 64-bit integer hashes of idempotency keys, avoiding heavy table lock overhead.
* **Dual Engine Architecture**:
  - `create_async_engine` with `asyncpg` driver for non-blocking FastAPI execution.
  - `create_engine` with `psycopg2-binary` for deterministic, synchronous Alembic migrations.

### 2.3 Alembic (Database Migrations)
* Versioned, reproducible schema evolution.
* Initial migration `0001_initial_payment_schema.py` establishes all 8 core tables with appropriate indexes, unique constraints, and foreign key relationships.

---

## 3. Core Architectural Modules

### 3.1 Relational Schema
The database schema consists of 8 tables:
1. `transactions`: Core ledger storing financial amounts as `BIGINT`, currencies, statuses, gateway references, and customer metadata.
2. `transaction_state_log`: Immutable, append-only audit trail logging every state transition, previous state, target state, actor, trigger event, and gateway payload.
3. `gateway_routes`: Rule configurations mapping currency, payment method, amount thresholds, and priority to target gateways.
4. `idempotency_keys`: Unique index on `(key, merchant_id)` caching response codes and response payloads for exactly-once execution.
5. `processed_webhook_events`: Composite primary key `(gateway, event_id)` for event deduplication and DLQ processing.
6. `gateway_health_metrics`: Real-time operational metrics (success rates, P95 latencies, circuit status).
7. `gateway_config`: Credentials vault references, base URLs, rate limits, and supported currencies/methods.
8. `routing_config`: Global and merchant-specific routing policies and thresholds.

### 3.2 Deterministic State Machine
The state machine strictly enforces 12 discrete states:
* Initial: `CREATED` -> `ROUTE_SELECTED`
* Authorization: `AUTH_INITIATED` -> `AUTHORISED` | `AUTH_FAILED` | `FAILED`
* Capture: `CAPTURE_INITIATED` -> `CAPTURED` | `PARTIALLY_CAPTURED` | `CAPTURE_FAILED`
* Refund: `REFUND_INITIATED` -> `REFUNDED`
* Terminal: `CAPTURED`, `REFUNDED`, `FAILED`

Transitions are executed using a **3-phase pattern**:
1. **Phase 1 (Lock Acquired)**: Pessimistic lock `SELECT ... FOR UPDATE` acquires row, validates transition from current to in-flight state (e.g. `ROUTE_SELECTED` -> `AUTH_INITIATED`), writes to `transaction_state_log`, commits, and **completely releases the lock**.
2. **Phase 2 (Lock Released)**: External HTTP call is made to the payment gateway (e.g. Stripe, Razorpay). Zero database locks are held during network latency.
3. **Phase 3 (Lock Re-acquired)**: Transaction row is re-acquired with `SELECT ... FOR UPDATE`, validates that no concurrent mutation occurred, records the final outcome (`AUTHORISED` or `AUTH_FAILED`), writes audit logs, and commits.

### 3.3 Idempotency Engine & Distributed Locking
* Clients submit a unique UUID header: `Idempotency-Key`.
* SHA-256 hash of `merchant_id:key` is computed and unpacked into a signed 64-bit integer.
* `SELECT pg_advisory_xact_lock(:lock_id)` is invoked inside the database transaction.
* If a request is already `PROCESSING`: Returns HTTP `409 Conflict`.
* If `COMPLETED`: Returns the cached HTTP status code and JSON payload directly.
* If payload hash does not match original request: Returns HTTP `422 Unprocessable Entity` (tamper protection).
* Expired keys (> 24 hours) are purged periodically.

### 3.4 Multi-Criteria Gateway Scoring & Failover
The `GatewayRouter` dynamically computes a suitability score for each candidate gateway:
$$\text{Score} = (W_{\text{success}} \times \text{NormSuccess}) + (W_{\text{latency}} \times (1 - \text{NormLatency})) + (W_{\text{cost}} \times (1 - \text{NormCost})) + (W_{\text{health}} \times \text{HealthScore}) + (W_{\text{fit}} \times \text{FitScore})$$

* $W_{\text{success}} = 0.35$ (35% Success Rate)
* $W_{\text{latency}} = 0.20$ (20% P95 Latency)
* $W_{\text{cost}} = 0.20$ (20% Cost Efficiency in basis points)
* $W_{\text{health}} = 0.15$ (15% Gateway Health & Circuit Breaker status)
* $W_{\text{fit}} = 0.10$ (10% Method and Currency compatibility)

If the primary gateway fails or times out, the router automatically fails over to the next highest-scoring gateway within 2 seconds.

### 3.5 Circuit Breaker Pattern
Each gateway is wrapped with an autonomous Circuit Breaker:
* **CLOSED**: Normal operation. Consecutive failures increment a counter. If failures reach the threshold (3 consecutive timeouts), the circuit trips to **OPEN**.
* **OPEN**: Fast-fail mode. All calls immediately fail without network I/O, allowing instantaneous failover to secondary gateways.
* **HALF-OPEN**: After a cooldown period (30 seconds), a trial canary request is permitted through. If successful, the circuit resets to **CLOSED**; if failed, it reverts to **OPEN**.

### 3.6 Webhook Ingestion & Dead Letter Queue
* **Constant-Time Verification**: Verifies incoming signatures with `hmac` and `hashlib.sha256` using `timingSafeEqual` (`hmac.compare_digest`) across Stripe, Razorpay, PayU, and UPI.
* **Deduplication**: Checks composite primary key `(gateway, event_id)` in `processed_webhook_events`. Duplicates return HTTP `200 OK` immediately without duplicate side-effects.
* **Dead Letter Queue (DLQ)**: If webhook processing fails 3 consecutive times, status moves to `DLQ` with full error diagnostic metadata preserved for manual replay.

### 3.7 Background Reconciliation Engine
* Periodically executes (every 15 minutes) as a non-blocking background task.
* Scans for transactions stuck in transient states (`AUTH_INITIATED`, `CAPTURE_INITIATED`) for longer than 5 minutes.
* Queries the authoritative gateway status endpoint.
* Resolves discrepancies and logs a `RECONCILIATION_OVERRIDE` audit event into `transaction_state_log`.

---

## 4. Production Deployment Architecture

```
                       +-------------------------------+
                       |        Docker Compose         |
                       |                               |
  Client Traffic ----->|  +-------------------------+  |
    (:8000)            |  |  zetheta_payment_api    |  |
                       |  |  (FastAPI / Python 3.13)|  |
                       |  +------------+------------+  |
                       |               |               |
                       |     Internal Docker Network   |
                       |               |               |
                       |  +------------v------------+  |
                       |  |   zetheta_postgres_16   |  |
                       |  |   (PostgreSQL 16 Alpine)|  |
                       |  +------------+------------+  |
                       |               |               |
                       |          [pgdata volume]      |
                       +-------------------------------+
```

The system is fully containerized via:
* `Dockerfile`: Multi-stage Python 3.13-slim build with non-root security user `appuser`.
* `docker-compose.yml`: Automated service composition linking FastAPI to PostgreSQL 16 with health check dependencies and volume persistence.

