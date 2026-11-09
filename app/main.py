from contextlib import asynccontextmanager
from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.database import async_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Ensure connection pool is responsive
    yield
    # Shutdown: Gracefully dispose database connection pools
    await async_engine.dispose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.PROJECT_VERSION,
    description=(
        "Mission-Critical Payment Orchestration Layer.\n\n"
        "### Financial Invariants:\n"
        "- **All monetary amounts are strictly BIGINT** (stored as minor units: paise, cents, pence).\n"
        "- **Immutable State Audit Trail**: Append-only transaction state ledger (`transaction_state_log`).\n"
        "- **Idempotency Engine**: Prevents double-charging and distributed race hazards.\n"
        "- **Webhook Deduplication**: Enforced via Composite Primary Key `(gateway, event_id)`."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Cross-Origin Resource Sharing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Application Routers
app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/health", status_code=status.HTTP_200_OK, tags=["System Health"])
async def health_check():
    """Liveness probe for orchestration and load balancer health checks."""
    return {
        "status": "healthy",
        "service": settings.PROJECT_NAME,
        "version": settings.PROJECT_VERSION,
        "environment": settings.ENVIRONMENT,
    }


@app.get("/", status_code=status.HTTP_200_OK, tags=["Root"])
async def root():
    return {
        "message": "Payment Orchestration Layer API is active.",
        "docs": "/docs",
        "health": "/health",
    }

