from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import WebhookProcessingStatus


class WebhookIngestRequest(BaseModel):
    """
    Standardized schema for incoming PSP webhook callbacks.
    """

    event_id: str = Field(..., max_length=255, examples=["evt_3L570Z2eZvKYlo2C1g9R"])
    event_type: str = Field(..., max_length=100, examples=["payment_intent.succeeded"])
    payload: dict[str, Any] = Field(..., description="Raw incoming webhook JSON payload")


class WebhookEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    gateway: str
    event_id: str
    event_type: str
    payload_hash: str
    status: WebhookProcessingStatus
    retry_count: int = 0
    raw_payload: dict[str, Any]
    processing_error: str | None = None
    received_at: datetime
    processed_at: datetime | None = None


class WebhookIngestResultResponse(BaseModel):
    status: str = Field(
        ..., description="Ingestion status: PROCESSED, duplicate_ignored, DLQ, etc."
    )
    gateway: str
    event_id: str
    retry_count: int = 0
    message: str | None = None
    processed_at: datetime | None = None
    error: str | None = None


class WebhookDLQEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    gateway: str
    event_id: str
    event_type: str
    payload_hash: str
    status: str
    retry_count: int
    raw_payload: dict[str, Any]
    processing_error: str | None = None
    received_at: datetime
    processed_at: datetime | None = None
