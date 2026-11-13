from datetime import datetime
from typing import Any, Dict, Optional
from pydantic import BaseModel, ConfigDict, Field
from app.core.enums import WebhookProcessingStatus


class WebhookIngestRequest(BaseModel):
    """
    Standardized schema for incoming PSP webhook callbacks.
    """
    event_id: str = Field(..., max_length=255, examples=["evt_3L570Z2eZvKYlo2C1g9R"])
    event_type: str = Field(..., max_length=100, examples=["payment_intent.succeeded"])
    payload: Dict[str, Any] = Field(..., description="Raw incoming webhook JSON payload")


class WebhookEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    gateway: str
    event_id: str
    event_type: str
    payload_hash: str
    status: WebhookProcessingStatus
    retry_count: int = 0
    raw_payload: Dict[str, Any]
    processing_error: Optional[str] = None
    received_at: datetime
    processed_at: Optional[datetime] = None


class WebhookIngestResultResponse(BaseModel):
    status: str = Field(..., description="Ingestion status: PROCESSED, duplicate_ignored, DLQ, etc.")
    gateway: str
    event_id: str
    retry_count: int = 0
    message: Optional[str] = None
    processed_at: Optional[datetime] = None
    error: Optional[str] = None


class WebhookDLQEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    gateway: str
    event_id: str
    event_type: str
    payload_hash: str
    status: str
    retry_count: int
    raw_payload: Dict[str, Any]
    processing_error: Optional[str] = None
    received_at: datetime
    processed_at: Optional[datetime] = None

