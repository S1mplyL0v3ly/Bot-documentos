"""Pydantic schemas for API request/response."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class DocumentStatus(BaseModel):
    document_id: int
    status: str
    source_channel: str
    sender_id: str
    original_filename: Optional[str]
    created_at: datetime
    missing_fields: list[str] = []
    output_available: bool = False


class FieldsUpdate(BaseModel):
    fields: dict[str, str]


class WebhookWhatsAppMessage(BaseModel):
    sender_id: str
    filename: Optional[str] = None
    media_url: Optional[str] = None
    message_text: Optional[str] = None


class WebhookEmailMessage(BaseModel):
    sender_email: str
    subject: Optional[str] = None
    filename: Optional[str] = None
    attachment_url: Optional[str] = None


class PipelineResult(BaseModel):
    document_id: int
    status: str
    document_type: Optional[str] = None
    missing_fields: list[str] = []
    message: str
