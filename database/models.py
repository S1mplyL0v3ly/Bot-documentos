"""SQLAlchemy models for autoreporte."""

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Document(Base):
    """Documento recibido por WhatsApp o email."""

    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    source_channel = Column(String(20), nullable=False)  # whatsapp | email
    sender_id = Column(String(255), nullable=False)
    original_filename = Column(String(512), nullable=True)
    status = Column(
        String(30),
        nullable=False,
        default="pending",
    )  # pending | processing | complete | error | waiting_fields

    extracted_fields = relationship(
        "ExtractedField", back_populates="document", cascade="all, delete-orphan"
    )
    generated_docx = relationship(
        "GeneratedDocx",
        back_populates="document",
        uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_documents_status", "status"),
        Index("ix_documents_created_at", "created_at"),
    )


class ExtractedField(Base):
    """Campo extraído de un documento."""

    __tablename__ = "extracted_fields"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)
    field_name = Column(String(255), nullable=False)
    field_value = Column(Text, nullable=True)
    confidence = Column(Float, default=0.0)  # 0.0 – 1.0
    source = Column(
        String(20), nullable=False, default="claude"
    )  # claude | scraper | manual

    document = relationship("Document", back_populates="extracted_fields")

    __table_args__ = (Index("ix_extracted_fields_document_id", "document_id"),)


class GeneratedDocx(Base):
    """DOCX generado a partir del documento procesado."""

    __tablename__ = "generated_docx"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(
        Integer, ForeignKey("documents.id"), nullable=False, unique=True
    )
    output_path = Column(String(512), nullable=False)
    generated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    fields_complete = Column(Boolean, default=False)
    _missing_fields = Column("missing_fields", Text, default="[]")

    document = relationship("Document", back_populates="generated_docx")

    @property
    def missing_fields(self) -> list[str]:
        return json.loads(self._missing_fields or "[]")

    @missing_fields.setter
    def missing_fields(self, value: list[str]) -> None:
        self._missing_fields = json.dumps(value)

    @property
    def output_path_obj(self) -> Path:
        return Path(self.output_path)

    __table_args__ = (Index("ix_generated_docx_document_id", "document_id"),)
