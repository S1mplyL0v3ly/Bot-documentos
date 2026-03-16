"""Pipeline orchestrator: coordinates classifier → extractor → scraper → docx_generator."""

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from agents.classifier import classify_document
from agents.extractor import extract_fields, read_document_text
from agents.scraper import fill_missing_fields
from config import JARVIS_DB_PATH, settings
from database import crud
from docx_generator.template_handler import render_template
from docx_generator.validator import validate_fields


def _log_to_jarvis(success: bool, document_id: int, notes: str = "") -> None:
    """Write pipeline result to JARVIS jarvis_metrics.db agent_actions table."""
    if not JARVIS_DB_PATH.exists():
        return
    try:
        with sqlite3.connect(str(JARVIS_DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO agent_actions
                    (agent_name, project, action, success, timestamp, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "autoreporte",
                    "autoreporte",
                    f"process_document:{document_id}",
                    1 if success else 0,
                    datetime.utcnow().isoformat(),
                    notes,
                ),
            )
    except sqlite3.Error:
        pass  # No bloquear el pipeline por fallo de log


async def process_document(
    db: Session,
    document_id: int,
    file_path: Path,
) -> dict:
    """Full pipeline: read → classify → extract → scrape → generate DOCX.

    Args:
        db: SQLAlchemy session.
        document_id: ID of the Document record.
        file_path: Path to the uploaded file.

    Returns:
        dict with status, missing_fields, output_path
    """
    crud.update_document_status(db, document_id, "processing")

    try:
        # 1. Leer texto del documento
        text = read_document_text(file_path)
        if not text:
            crud.update_document_status(db, document_id, "error")
            _log_to_jarvis(False, document_id, "empty_text")
            return {
                "status": "error",
                "reason": "No se pudo extraer texto del documento.",
            }

        # 2. Clasificar tipo y campos esperados
        classification = classify_document(text)
        expected_fields: list[str] = classification.get("expected_fields", [])

        # 3. Extraer campos
        extracted = extract_fields(text, expected_fields)

        # Guardar en BD
        for field_name, field_data in extracted.items():
            crud.upsert_field(
                db,
                document_id=document_id,
                field_name=field_name,
                field_value=field_data.get("value"),
                confidence=float(field_data.get("confidence", 0.0)),
                source="claude",
            )

        # 4. Detectar campos faltantes
        fields_dict = {k: v.get("value") for k, v in extracted.items()}
        validation = validate_fields(fields_dict, expected_fields)
        missing = validation.get("missing", [])

        # 5. Intentar rellenar campos faltantes con scraper
        if missing:
            scraped = await fill_missing_fields(missing, fields_dict)
            for field_name, value in scraped.items():
                if value is not None:
                    crud.upsert_field(
                        db,
                        document_id=document_id,
                        field_name=field_name,
                        field_value=value,
                        confidence=0.5,
                        source="scraper",
                    )
                    fields_dict[field_name] = value
                    missing.remove(field_name)

        # 6. Generar DOCX
        all_fields = {
            f.field_name: f.field_value for f in crud.get_fields(db, document_id)
        }
        output_path = render_template(document_id, all_fields)

        fields_complete = len(missing) == 0
        crud.create_or_update_generated_docx(
            db,
            document_id=document_id,
            output_path=str(output_path),
            fields_complete=fields_complete,
            missing_fields=missing,
        )

        final_status = "complete" if fields_complete else "waiting_fields"
        crud.update_document_status(db, document_id, final_status)
        _log_to_jarvis(True, document_id, f"missing={len(missing)}")

        return {
            "status": final_status,
            "document_type": classification.get("document_type"),
            "missing_fields": missing,
            "output_path": str(output_path),
        }

    except Exception as exc:
        crud.update_document_status(db, document_id, "error")
        _log_to_jarvis(False, document_id, str(exc)[:200])
        raise
