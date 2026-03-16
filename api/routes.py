"""FastAPI route definitions."""

import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from agents.orchestrator import process_document
from api.schemas import DocumentStatus, FieldsUpdate, PipelineResult
from config import BASE_DIR
from database import crud
from database.init_db import init_db
from database.models import Document

UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

router = APIRouter()


def get_db():
    """Dependency: yield SQLAlchemy session."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from config import settings

    engine = create_engine(settings.database_url)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _save_upload(upload: UploadFile) -> Path:
    """Persist uploaded file to disk and return its path."""
    safe_name = Path(upload.filename or "document").name
    dest = UPLOAD_DIR / safe_name
    dest.write_bytes(upload.file.read())
    return dest


async def _run_pipeline(db: Session, document_id: int, file_path: Path) -> None:
    """Background task wrapper for the pipeline."""
    await process_document(db, document_id, file_path)


@router.post("/webhook/whatsapp", response_model=PipelineResult, status_code=202)
async def webhook_whatsapp(
    background_tasks: BackgroundTasks,
    sender_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Receive a document from WhatsApp and start processing."""
    file_path = _save_upload(file)
    doc = crud.create_document(db, "whatsapp", sender_id, file.filename)
    background_tasks.add_task(_run_pipeline, db, doc.id, file_path)
    return PipelineResult(
        document_id=doc.id,
        status="pending",
        message=f"Documento recibido. Procesando en background. ID={doc.id}",
    )


@router.post("/webhook/email", response_model=PipelineResult, status_code=202)
async def webhook_email(
    background_tasks: BackgroundTasks,
    sender_email: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Receive a document from email and start processing."""
    file_path = _save_upload(file)
    doc = crud.create_document(db, "email", sender_email, file.filename)
    background_tasks.add_task(_run_pipeline, db, doc.id, file_path)
    return PipelineResult(
        document_id=doc.id,
        status="pending",
        message=f"Documento recibido vía email. Procesando. ID={doc.id}",
    )


@router.get("/status/{doc_id}", response_model=DocumentStatus)
def get_status(doc_id: int, db: Session = Depends(get_db)):
    """Return current processing status of a document."""
    doc = crud.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")

    missing: list[str] = []
    output_available = False
    if doc.generated_docx:
        missing = doc.generated_docx.missing_fields
        output_available = Path(doc.generated_docx.output_path).exists()

    return DocumentStatus(
        document_id=doc.id,
        status=doc.status,
        source_channel=doc.source_channel,
        sender_id=doc.sender_id,
        original_filename=doc.original_filename,
        created_at=doc.created_at,
        missing_fields=missing,
        output_available=output_available,
    )


@router.post("/fields/{doc_id}", response_model=PipelineResult)
async def update_fields(
    doc_id: int,
    payload: FieldsUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Accept manually provided field values and re-trigger DOCX generation."""
    doc = crud.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")

    for field_name, value in payload.fields.items():
        crud.upsert_field(
            db, doc_id, field_name, value, confidence=1.0, source="manual"
        )

    # Re-generar DOCX con los nuevos campos
    upload_dir = BASE_DIR / "uploads"
    file_candidates = list(upload_dir.glob(f"*"))
    file_path = file_candidates[0] if file_candidates else Path("/dev/null")
    background_tasks.add_task(_run_pipeline, db, doc_id, file_path)

    return PipelineResult(
        document_id=doc_id,
        status="processing",
        message="Campos actualizados. Re-generando DOCX.",
    )


@router.get("/download/{doc_id}")
def download_docx(doc_id: int, db: Session = Depends(get_db)):
    """Download the generated DOCX for a document."""
    doc = crud.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")
    if doc.generated_docx is None:
        raise HTTPException(status_code=404, detail="DOCX aún no generado.")

    output_path = Path(doc.generated_docx.output_path)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado en disco.")

    return FileResponse(
        path=str(output_path),
        filename=output_path.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
