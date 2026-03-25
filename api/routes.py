"""FastAPI route definitions."""

import asyncio
import uuid
from datetime import datetime
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
    handle_web_confirmation,
    proceed_after_web,
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

# Deduplication: track WhatsApp message IDs already dispatched to background tasks.
# Prevents double-processing when Meta delivers the same webhook twice.
_seen_msg_ids: set[str] = set()
_MAX_SEEN = 2000  # cap to avoid unbounded growth

router = APIRouter()


def _make_db():
    """Create a fresh SQLAlchemy session for background tasks.

    Background tasks outlive the request context, so they must NOT reuse the
    request-scoped session from Depends(get_db) — that session is closed when
    the response is sent.  Each background task calls _make_db() at the top
    and closes the session in its own finally block.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from config import settings

    engine = create_engine(
        settings.database_url, connect_args={"check_same_thread": False}
    )
    return sessionmaker(bind=engine)()


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


_SAFE_UPLOAD_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".txt",
    ".md",
    ".csv",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
}


def _save_upload(upload: UploadFile) -> Path:
    """Persist uploaded file to disk with a randomized name to prevent traversal."""
    original = Path(upload.filename or "document")
    extension = original.suffix.lower()
    if extension not in _SAFE_UPLOAD_EXTENSIONS:
        raise HTTPException(400, f"File type {extension!r} not allowed")
    # Use UUID-based name to prevent path traversal and filename collisions
    safe_name = f"{uuid.uuid4().hex[:16]}{extension}"
    dest = UPLOAD_DIR / safe_name
    # Verify dest stays within UPLOAD_DIR (symlink escape guard)
    if not dest.resolve().is_relative_to(UPLOAD_DIR.resolve()):
        raise HTTPException(400, "Invalid file path")
    dest.write_bytes(upload.file.read())
    return dest


async def _run_pipeline(db: Session, document_id: int, file_path: Path) -> None:
    """Background task: FASE 1 → auto-triggers FASE 2/3 as needed."""
    await process_document(db, document_id, file_path)


async def _bg_process_wa_document(
    document_id: int, media_id: str, dest_path: Path, sender: str
) -> None:
    """Background: download first doc (cuestionario), store path, ask for consultor data."""
    from channels.whatsapp import download_media
    from channels.whatsapp import send_text

    db = _make_db()
    ok = await download_media(media_id, str(dest_path))
    if not ok:
        crud.update_document_status(db, document_id, "error")
        db.close()
        await send_text(
            sender, "No pude descargar tu archivo. Por favor inténtalo de nuevo."
        )
        return

    # CAMBIO 1: store cuestionario path + today's date, move to waiting_transcript
    crud.upsert_field(
        db, document_id, "_file_path", str(dest_path), confidence=1.0, source="system"
    )
    crud.upsert_field(
        db,
        document_id,
        "dir_Reunion_Inicial",
        datetime.now().strftime("%d/%m/%Y"),
        confidence=1.0,
        source="system",
    )
    crud.update_document_status(db, document_id, "waiting_transcript")
    db.close()

    await send_text(
        sender,
        "📄 *Cuestionario recibido.*\n\n"
        "Para continuar, dime:\n"
        "*¿Cuál es el nombre del consultor y la fecha de la reunión?*\n\n"
        "_Ej: María González, 15/03/2025_\n\n"
        "_(En el siguiente paso te pediré el cargo de la persona de contacto)_",
    )


async def _bg_process_wa_transcript(
    document_id: int, media_id: str, dest_path: Path, sender: str
) -> None:
    """Background: download transcript PDF, run full pipeline with both docs."""
    from agents.extractor import read_document_text
    from channels.whatsapp import download_media
    from channels.whatsapp import send_text

    db = _make_db()
    ok = await download_media(media_id, str(dest_path))
    if not ok:
        db.close()
        await send_text(
            sender, "No pude descargar la transcripción. Inténtalo de nuevo."
        )
        return

    transcript_text = read_document_text(dest_path)

    # Save transcript to document record
    crud.save_transcript_text(db, document_id, transcript_text)
    crud.update_document_status(db, document_id, "processing")

    # Retrieve stored cuestionario path
    fields = crud.get_fields(db, document_id)
    cuestionario_path_str = next(
        (f.field_value for f in fields if f.field_name == "_file_path"), None
    )
    if not cuestionario_path_str:
        db.close()
        await send_text(sender, "Error interno: no encontré el cuestionario original.")
        return

    await send_text(
        sender,
        "⏳ *Procesando ambos documentos con IA...*\n"
        "El proceso tarda aproximadamente *2-3 minutos*.\n\n"
        "Te avisaré cuando esté listo. 🔄",
    )
    await asyncio.sleep(2)

    result = await process_document(
        db, document_id, Path(cuestionario_path_str), transcript_text=transcript_text
    )
    db.close()
    status = result.get("status")

    if status in ("waiting_web_confirmation", "waiting_web_url"):
        await send_text(sender, result["question_message"])
    elif status == "waiting_user_response":
        await send_text(sender, result["question_message"])
    elif status == "waiting_approval":
        await send_text(sender, result["draft_message"])
    elif status == "error":
        await send_text(sender, "Error procesando los documentos. Inténtalo de nuevo.")


async def _bg_process_wa_text(sender: str, text: str) -> None:
    """Background: route incoming WhatsApp text to the correct pipeline stage."""
    from channels.whatsapp import send_document as wa_send_document
    from channels.whatsapp import send_text

    db = _make_db()
    text_lower = text.lower().strip()

    # CAMBIO 4: nombre consultor + cargo + fecha reunión (doc waiting for transcript)
    waiting_doc = crud.get_document_waiting_transcript(db, sender)
    if waiting_doc:
        fields = crud.get_fields(db, waiting_doc.id)
        has_consultor = any(f.field_name == "dir_Nombre_realizador" for f in fields)
        has_cargo = any(f.field_name == "dir_Cargo" for f in fields)
        if not has_consultor:
            # FIX 1: split "Nombre, DD/MM/YYYY" into name + date
            if "," in text:
                parts = text.split(",", 1)
                nombre_part = parts[0].strip()
                fecha_part = parts[1].strip()
            else:
                nombre_part = text.strip()
                fecha_part = ""
            crud.upsert_field(
                db,
                waiting_doc.id,
                "dir_Nombre_realizador",
                nombre_part,
                confidence=1.0,
                source="manual",
            )
            if fecha_part:
                crud.upsert_field(
                    db,
                    waiting_doc.id,
                    "dir_Reunion_Inicial",
                    fecha_part,
                    confidence=1.0,
                    source="manual",
                )
            await send_text(
                sender,
                "✅ *Guardado.*\n\n"
                "*¿Cuál es el cargo de la persona de contacto?*\n\n"
                "_Ej: Gerente / Director Comercial / Autónomo/a_",
            )
            return
        if not has_cargo:
            crud.upsert_field(
                db,
                waiting_doc.id,
                "dir_Cargo",
                text,
                confidence=1.0,
                source="manual",
            )
            await send_text(
                sender,
                "✅ *Guardado.*\n\n"
                "Ahora envíame la *transcripción de la entrevista* en PDF.\n"
                "_También puedes escribirla directamente aquí como texto._",
            )
            return

        # Consultor + cargo ya guardados → usuario envía transcript como texto libre
        fields_map = crud.get_fields(db, waiting_doc.id)
        cuestionario_path_str = next(
            (f.field_value for f in fields_map if f.field_name == "_file_path"), None
        )
        if not cuestionario_path_str:
            await send_text(
                sender, "Error interno: no encontré el cuestionario original."
            )
            return

        crud.save_transcript_text(db, waiting_doc.id, text)
        crud.update_document_status(db, waiting_doc.id, "processing")
        await send_text(
            sender,
            "⏳ *Procesando documentos con IA...*\n"
            "El proceso tarda aproximadamente *2-3 minutos*. 🔄",
        )
        await asyncio.sleep(2)

        result = await process_document(
            db, waiting_doc.id, Path(cuestionario_path_str), transcript_text=text
        )
        status = result.get("status")
        if status in ("waiting_web_confirmation", "waiting_web_url"):
            await send_text(sender, result["question_message"])
        elif status == "waiting_user_response":
            await send_text(sender, result["question_message"])
        elif status == "waiting_approval":
            await send_text(sender, result["draft_message"])
        elif status == "error":
            await send_text(
                sender, "Error procesando los documentos. Inténtalo de nuevo."
            )
        return

    # Web confirmation — waiting_web_confirmation or waiting_web_url
    for web_status in ("waiting_web_confirmation", "waiting_web_url"):
        web_doc = crud.get_document_by_sender_and_status(db, sender, web_status)
        if web_doc:
            result = await handle_web_confirmation(db, web_doc.id, text, sender)
            status = result.get("status")
            if status in ("confirmed_url", "confirmed_no_web"):
                # Web step resolved → continue with questions
                next_result = await proceed_after_web(db, web_doc.id)
                next_status = next_result.get("status")
                if next_status == "waiting_user_response":
                    await send_text(sender, next_result["question_message"])
                elif next_status == "waiting_approval":
                    await send_text(sender, next_result["draft_message"])
            elif status == "waiting_web_url":
                await send_text(sender, result["message"])
            elif status == "unrecognised":
                await send_text(
                    sender,
                    "No entendí tu respuesta. Por favor responde:\n"
                    "• *CONFIRMAR* — la web es correcta\n"
                    "• *NO TIENE WEB* — la empresa no tiene página\n"
                    "• *OTRA WEB https://...* — indicando la URL correcta",
                )
            return

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
    db.close()
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
):
    """Receive Meta WhatsApp webhook events and route to the correct handler.

    Meta requires a 200 OK response within 5 seconds. All processing is done
    in background tasks that open their own DB sessions via _make_db().
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
    msg_id = msg.get("id", "")
    sender = msg.get("from", "")
    msg_type = msg.get("type", "")

    # Deduplicate: Meta sometimes delivers the same webhook twice.
    global _seen_msg_ids
    if msg_id and msg_id in _seen_msg_ids:
        return {"status": "ok"}
    if msg_id:
        _seen_msg_ids.add(msg_id)
        if len(_seen_msg_ids) > _MAX_SEEN:
            # Evict oldest entries by rebuilding a smaller set
            _seen_msg_ids = set(list(_seen_msg_ids)[-_MAX_SEEN // 2 :])

    if msg_type in ("document", "image"):
        media_info = msg.get(msg_type, {})
        media_id = media_info.get("id", "")
        filename = media_info.get("filename") or f"documento_{int(time.time())}"
        mime_type = media_info.get("mime_type", "")
        ext = _MIME_TO_EXT.get(mime_type, "")
        if ext and not filename.endswith(ext):
            filename = f"{Path(filename).stem}{ext}"

        dest_path = UPLOAD_DIR / f"{int(time.time())}_{filename}"

        # CAMBIO 1: check if sender is already waiting for a transcript
        # Use a short-lived session just for this synchronous lookup.
        _db = _make_db()
        waiting_doc = crud.get_document_waiting_transcript(_db, sender)
        if waiting_doc:
            _doc_id = waiting_doc.id
            _db.close()
            background_tasks.add_task(
                _bg_process_wa_transcript,
                _doc_id,
                media_id,
                dest_path,
                sender,
            )
        else:
            doc = crud.create_document(_db, "whatsapp", sender, filename)
            _doc_id = doc.id
            _db.close()
            background_tasks.add_task(
                _bg_process_wa_document, _doc_id, media_id, dest_path, sender
            )

    elif msg_type == "text":
        text = msg.get("text", {}).get("body", "")
        if text:
            background_tasks.add_task(_bg_process_wa_text, sender, text)

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
