from fastapi import APIRouter
from app.api.v1.endpoints import gateways, transactions, webhooks

api_router = APIRouter()

api_router.include_router(transactions.router, prefix="/transactions", tags=["Transactions"])
api_router.include_router(webhooks.router, prefix="/webhooks", tags=["Webhooks & Deduplication"])
api_router.include_router(gateways.router, prefix="/gateways", tags=["Gateways & Smart Routing"])

