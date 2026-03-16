"""FastAPI route definitions."""

from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.orm import Session

from agents.orchestrator import (
    apply_draft_changes,
    generate_final_docx,
    generate_questions,
    process_document,
    process_user_response,
)
from api.schemas import ApprovalPayload, DocumentStatus, FieldsUpdate, PipelineResult
from config import BASE_DIR, settings
from database import crud
from database.init_db import init_db

UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# MIME type → file extension map for WhatsApp media
_MIME_TO_EXT: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}

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


async def _bg_process_wa_document(
    db: Session, document_id: int, media_id: str, dest_path: Path, sender: str
) -> None:
    """Background: download WhatsApp media, run pipeline, reply to sender."""
    from channels.whatsapp import download_media, send_document as wa_send_document
    from channels.whatsapp import send_text

    ok = await download_media(media_id, str(dest_path))
    if not ok:
        crud.update_document_status(db, document_id, "error")
        await send_text(
            sender, "No pude descargar tu archivo. Por favor inténtalo de nuevo."
        )
        return

    await send_text(
        sender,
        "📄 *Documento recibido correctamente*\n\n"
        "⏳ Estoy analizando el documento con IA...\n"
        "El proceso tarda aproximadamente *2-3 minutos*.\n\n"
        "Te avisaré cuando esté listo. 🔄",
    )

    result = await process_document(db, document_id, dest_path)
    status = result.get("status")

    if status == "waiting_user_response":
        await send_text(sender, result["question_message"])
    elif status == "waiting_approval":
        await send_text(sender, result["draft_message"])
    elif status == "error":
        await send_text(sender, "Error procesando el documento. Inténtalo de nuevo.")


async def _bg_process_wa_text(db: Session, sender: str, text: str) -> None:
    """Background: route incoming WhatsApp text to the correct pipeline stage."""
    from channels.whatsapp import send_document as wa_send_document
    from channels.whatsapp import send_text

    text_lower = text.lower().strip()

    # Approval keywords → FASE 4
    if any(kw in text_lower for kw in ("apruebo", "aprobado", "aprobar")):
        doc = crud.get_document_by_sender_and_status(db, sender, "waiting_approval")
        if doc:
            result = generate_final_docx(db, doc.id)
            if result["status"] == "complete":
                output_path = result["output_path"]
                razon_social = result.get("empresa_name", "tu empresa")
                await send_text(
                    sender,
                    "✅ *¡Informe finalizado!*\n\n"
                    f"📊 *Informe DPI — {razon_social}*\n\n"
                    "Te envío ahora el documento Word (.docx) con el\n"
                    "informe completo de diagnóstico de potencial\n"
                    "de internacionalización.\n\n"
                    "📎 _El archivo se descargará automáticamente._",
                )
                await wa_send_document(sender, output_path, Path(output_path).name)
        else:
            await send_text(sender, "No tengo ningún informe pendiente de aprobación.")
        return

    # User answering questions → FASE 2b
    doc = crud.get_document_by_sender_and_status(db, sender, "waiting_user_response")
    if doc:
        result = await process_user_response(db, doc.id, text)
        status = result.get("status")
        if status == "waiting_user_response":
            await send_text(sender, result.get("question_message", ""))
        elif status == "waiting_approval":
            await send_text(sender, result.get("draft_message", ""))
        elif status == "error":
            await send_text(sender, "Error procesando tus respuestas.")
        return

    # No active document found
    await send_text(
        sender, "Hola, envíame un documento PDF o Word para generar tu informe DPI."
    )


# ─── WEBHOOKS ─────────────────────────────────────────────────────────────────


@router.get("/webhook/whatsapp")
async def verify_whatsapp_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Webhook verification endpoint required by Meta."""
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return PlainTextResponse(content=hub_challenge, status_code=200)

    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/webhook/whatsapp", status_code=200)
async def webhook_whatsapp(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Receive Meta WhatsApp webhook events and route to the correct handler.

    Meta requires a 200 OK response within 5 seconds. All processing is done
    in background tasks.
    """
    import time

    try:
        body = await request.json()
    except Exception:
        return {"status": "ok"}

    # Navigate Meta's nested structure: entry → changes → value → messages
    try:
        messages = (
            body.get("entry", [{}])[0]
            .get("changes", [{}])[0]
            .get("value", {})
            .get("messages", [])
        )
    except (IndexError, AttributeError, KeyError):
        return {"status": "ok"}

    if not messages:
        return {"status": "ok"}

    msg = messages[0]
    sender = msg.get("from", "")
    msg_type = msg.get("type", "")

    if msg_type in ("document", "image"):
        media_info = msg.get(msg_type, {})
        media_id = media_info.get("id", "")
        filename = media_info.get("filename") or f"documento_{int(time.time())}"
        mime_type = media_info.get("mime_type", "")
        ext = _MIME_TO_EXT.get(mime_type, "")
        if ext and not filename.endswith(ext):
            filename = f"{Path(filename).stem}{ext}"

        dest_path = UPLOAD_DIR / f"{int(time.time())}_{filename}"
        doc = crud.create_document(db, "whatsapp", sender, filename)
        background_tasks.add_task(
            _bg_process_wa_document, db, doc.id, media_id, dest_path, sender
        )

    elif msg_type == "text":
        text = msg.get("text", {}).get("body", "")
        if text:
            background_tasks.add_task(_bg_process_wa_text, db, sender, text)

    return {"status": "ok"}


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
