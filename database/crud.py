"""CRUD operations for autoreporte database."""

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from database.models import Document, ExtractedField, GeneratedDocx

# --- Document ---


def create_document(
    db: Session,
    source_channel: str,
    sender_id: str,
    original_filename: Optional[str] = None,
) -> Document:
    """Create a new document record with status=pending."""
    doc = Document(
        source_channel=source_channel,
        sender_id=sender_id,
        original_filename=original_filename,
        status="pending",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def get_document(db: Session, document_id: int) -> Optional[Document]:
    """Fetch a document by ID."""
    return db.get(Document, document_id)


def get_document_waiting_transcript(db: Session, sender_id: str) -> Optional[Document]:
    """Return a document in waiting_transcript status for the given sender."""
    return get_document_by_sender_and_status(db, sender_id, "waiting_transcript")


def save_transcript_text(
    db: Session, document_id: int, transcript_text: str
) -> Optional[Document]:
    """Persist transcript text into the document record."""
    doc = db.get(Document, document_id)
    if doc is None:
        return None
    doc.transcript_text = transcript_text
    db.commit()
    db.refresh(doc)
    return doc


def get_document_by_sender_and_status(
    db: Session, sender_id: str, status: str
) -> Optional[Document]:
    """Return the most recent document for a sender that matches the given status."""
    return (
        db.query(Document)
        .filter_by(sender_id=sender_id, status=status)
        .order_by(Document.id.desc())
        .first()
    )


def update_document_status(
    db: Session, document_id: int, status: str
) -> Optional[Document]:
    """Update the processing status of a document."""
    doc = db.get(Document, document_id)
    if doc is None:
        return None
    doc.status = status
    db.commit()
    db.refresh(doc)
    return doc


# --- ExtractedField ---


def upsert_field(
    db: Session,
    document_id: int,
    field_name: str,
    field_value: Optional[str],
    confidence: float = 0.0,
    source: str = "claude",
) -> ExtractedField:
    """Insert or update an extracted field for a document."""
    field = (
        db.query(ExtractedField)
        .filter_by(document_id=document_id, field_name=field_name)
        .first()
    )
    if field is None:
        field = ExtractedField(
            document_id=document_id,
            field_name=field_name,
            field_value=field_value,
            confidence=confidence,
            source=source,
        )
        db.add(field)
    else:
        field.field_value = field_value
        field.confidence = confidence
        field.source = source
    db.commit()
    db.refresh(field)
    return field


def get_fields(db: Session, document_id: int) -> list[ExtractedField]:
    """Get all extracted fields for a document."""
    return db.query(ExtractedField).filter_by(document_id=document_id).all()


# --- GeneratedDocx ---


def create_or_update_generated_docx(
    db: Session,
    document_id: int,
    output_path: str,
    fields_complete: bool,
    missing_fields: list[str],
) -> GeneratedDocx:
    """Insert or update the generated DOCX record."""
    record = db.query(GeneratedDocx).filter_by(document_id=document_id).first()
    if record is None:
        record = GeneratedDocx(
            document_id=document_id,
            output_path=output_path,
            generated_at=datetime.utcnow(),
            fields_complete=fields_complete,
        )
        record.missing_fields = missing_fields
        db.add(record)
    else:
        record.output_path = output_path
        record.generated_at = datetime.utcnow()
        record.fields_complete = fields_complete
        record.missing_fields = missing_fields
    db.commit()
    db.refresh(record)
    return record
