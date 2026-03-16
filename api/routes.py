"""FastAPI route definitions."""

from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from agents.orchestrator import (
    apply_draft_changes,
    generate_final_docx,
    generate_questions,
    process_document,
)
from api.schemas import ApprovalPayload, DocumentStatus, FieldsUpdate, PipelineResult
from config import BASE_DIR
from database import crud
from database.init_db import init_db

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
    """Background task: FASE 1 → auto-triggers FASE 2/3 as needed."""
    await process_document(db, document_id, file_path)


# ─── WEBHOOKS ─────────────────────────────────────────────────────────────────


@router.post("/webhook/whatsapp", response_model=PipelineResult, status_code=202)
async def webhook_whatsapp(
    background_tasks: BackgroundTasks,
    sender_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Receive a document from WhatsApp and start FASE 1 processing."""
    file_path = _save_upload(file)
    doc = crud.create_document(db, "whatsapp", sender_id, file.filename)
    background_tasks.add_task(_run_pipeline, db, doc.id, file_path)
    return PipelineResult(
        document_id=doc.id,
        status="pending",
        message=f"Documento recibido por WhatsApp. Procesando. ID={doc.id}",
    )


@router.post("/webhook/email", response_model=PipelineResult, status_code=202)
async def webhook_email(
    background_tasks: BackgroundTasks,
    sender_email: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Receive a document from email and start FASE 1 processing."""
    file_path = _save_upload(file)
    doc = crud.create_document(db, "email", sender_email, file.filename)
    background_tasks.add_task(_run_pipeline, db, doc.id, file_path)
    return PipelineResult(
        document_id=doc.id,
        status="pending",
        message=f"Documento recibido vía email. Procesando. ID={doc.id}",
    )


# ─── STATUS ───────────────────────────────────────────────────────────────────


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


# ─── FASE 2: User answers missing criteria ────────────────────────────────────


@router.post("/fields/{doc_id}", response_model=PipelineResult)
async def update_fields(
    doc_id: int,
    payload: FieldsUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """FASE 2 response: user provides answers for null DPI criteria.

    Expects {criterion_key: selected_option} in payload.fields.
    After saving, triggers FASE 3 (draft generation) in background.
    """
    doc = crud.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")

    from agents.orchestrator import PREFIX_SELECTION, generate_draft_texts

    for criterion_key, option_value in payload.fields.items():
        crud.upsert_field(
            db,
            doc_id,
            f"{PREFIX_SELECTION}{criterion_key}",
            option_value,
            confidence=1.0,
            source="manual",
        )

    # Trigger FASE 3 in background
    background_tasks.add_task(generate_draft_texts, db, doc_id)

    remaining = generate_questions(db, doc_id)
    return PipelineResult(
        document_id=doc_id,
        status="processing",
        message="Respuestas guardadas. Generando borrador DAFO y textos.",
        question_message=remaining or None,
    )


# ─── FASE 3: Approval ─────────────────────────────────────────────────────────


@router.post("/approve/{doc_id}", response_model=PipelineResult)
async def approve_draft(
    doc_id: int,
    payload: ApprovalPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """FASE 3 → FASE 4: Approve draft or request changes.

    - approved=true → generates final DOCX immediately.
    - approved=false + changes → Claude revises the draft and sends it back.
    """
    doc = crud.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado.")

    if doc.status not in {"waiting_approval", "complete"}:
        raise HTTPException(
            status_code=400,
            detail=f"El documento está en estado '{doc.status}', no puede aprobarse aún.",
        )

    if payload.approved:
        result = generate_final_docx(db, doc_id)
        return PipelineResult(
            document_id=doc_id,
            status=result["status"],
            message="Informe DPI generado. Descárgalo en /download/{doc_id}.",
        )

    # Not approved → apply changes and re-send draft
    if not payload.changes:
        raise HTTPException(
            status_code=400,
            detail="Si no apruebas el borrador, indica los cambios en el campo 'changes'.",
        )

    async def _apply_changes_bg():
        await apply_draft_changes(db, doc_id, payload.changes)

    background_tasks.add_task(_apply_changes_bg)
    return PipelineResult(
        document_id=doc_id,
        status="waiting_approval",
        message="Aplicando tus cambios al borrador. Recibirás el borrador revisado en breve.",
    )


# ─── DOWNLOAD ─────────────────────────────────────────────────────────────────


@router.get("/download/{doc_id}")
def download_docx(doc_id: int, db: Session = Depends(get_db)):
    """Download the final generated DOCX."""
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
        headers={"Content-Disposition": f'attachment; filename="{output_path.name}"'},
    )
