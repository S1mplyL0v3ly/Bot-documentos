"""Pipeline orchestrator: 4-phase DPI document processing."""

import json
import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from agents.extractor import CRITERION_OPTIONS, extract_dpi_fields, read_document_text
from agents.web_scraper import find_best_candidate
from config import JARVIS_DB_PATH, settings
from database import crud
from database.models import Document
from docx_generator.template_handler import render_template

# Human-readable questions for each DPI criterion
CRITERION_QUESTIONS: dict[str, str] = {
    "situacion_empresa": "¿Cuál es la situación de la empresa?\n   → No constituida / Menos de 2 años / Más de 2 años",
    "num_empleados": "¿Cuántos empleados tiene la empresa?\n   → Menos de 2 / Más de 2",
    "facturacion": "¿Cuál es la facturación anual?\n   → Menos de 200.000€ / Entre 200.000-500.000€ / Entre 500.000-1.000.000€ / Más de 1.000.000€",
    "evolucion_facturacion": "¿Cómo ha evolucionado la facturación?\n   → En decrecimiento / Estable / En crecimiento",
    "recursos_internacionalizacion": "¿Dispone de recursos para un plan de internacionalización?\n   → No / Sí",
    "experiencia_internacional": "¿Cuánta experiencia internacional tiene?\n   → Ninguna / Menos de 3 años / Más de 5 años",
    "alcance_actividad": "¿Cuál es el alcance actual de la actividad comercial?\n   → Insular / Nacional / Internacional",
    "num_paises": "¿En cuántos países vende regularmente?\n   → Ninguno / De 1 a 5 / Más de 5",
    "personal_dedicado": "¿Tiene personal dedicado exclusivamente a comercio exterior?\n   → No / Sí",
    "involuccion_gerencia": "¿Cómo está involucrada la gerencia en la actividad internacional?\n   → Sin participación / Escasamente involucrados / Medianamente involucrados / Directamente involucrados",
    "adaptacion_demanda": "¿En qué medida adapta la oferta a la demanda internacional?\n   → Baja / Media / Alta",
    "adaptacion_producto": "¿En qué medida adapta el producto para mercados internacionales?\n   → Baja / Media / Alta",
    "tiene_web": "¿Dispone de página web corporativa?\n   → No / Sí",
    "ecommerce": "¿Tiene tienda online?\n   → Sin tienda web propia / Tienda web propia con ventas bajas / Tienda web propia con ventas regulares a nivel nacional / Tienda web propia con ventas regulares a nivel internacional",
    "mercados_electronicos": "¿Está presente en mercados electrónicos (Amazon, Alibaba, etc.)?\n   → Sin presencia en mercados electrónicos / Con presencia pero sin ventas / Con ventas nacionales / Con ventas internacionales",
    "redes_sociales": "¿Cuál es el nivel de actividad en redes sociales?\n   → Redes sociales inactivas o inexistentes / Redes sociales activas y planificadas / Redes sociales que generan ventas",
}

QUESTION_BATCH_SIZE = 7  # Max criteria per WhatsApp message

# ─── DPI Scoring (CAMBIO 6) ───────────────────────────────────────────────────

DPI_SCORE_MAP: dict[str, dict[str, int]] = {
    # BLOQUE ECONÓMICO (15 pts)
    "situacion_empresa": {
        "No constituida": 0,
        "Menos de 2 años": 2,
        "Más de 2 años": 5,
    },
    "num_empleados": {"Menos de 2": 1, "Más de 2": 5},
    "facturacion": {
        "Menos de 200.000 €": 1,
        "Entre 200.000 y 500.000 €": 2,
        "Entre 500.000 y 1.000.000 €": 4,
        "Más de 1.000.000 €": 5,
    },
    # BLOQUE INTERNACIONAL (35 pts)
    "experiencia_internacional": {
        "Ninguna experiencia": 0,
        "Menos de 3 años": 4,
        "Más de 5 años": 10,
    },
    "alcance_actividad": {"Insular": 1, "Nacional": 3, "Internacional": 6},
    "evolucion_facturacion": {
        "En decrecimiento": 0,
        "Se mantiene estable": 3,
        "En crecimiento": 6,
    },
    "involuccion_gerencia": {
        "Sin participación alguna": 0,
        "Escasamente involucrados": 1,
        "Medianamente involucrados": 3,
        "Directamente involucrados": 5,
    },
    "adaptacion_demanda": {"Baja": 0, "Media": 2, "Alta": 4},
    "adaptacion_producto": {"Baja": 0, "Media": 1, "Alta": 2},
    "num_paises": {
        "Ninguno salvo el mercado nacional": 0,
        "De 1 a 5": 1,
        "Más de 5": 2,
    },
    # BLOQUE DIGITALIZACIÓN (15 pts)
    "tiene_web": {"No": 0, "Si": 3},
    "ecommerce": {
        "Sin tienda web": 1,
        "Tienda web propia con ventas bajas o irregulares": 2,
        "Tienda web propia con ventas regulares a nivel nacional": 4,
        "Tienda web propia con ventas internacionales": 6,
    },
    "mercados_electronicos": {
        "Sin presencia en mercados electrónicos": 0,
        "Con presencia en mercados electrónicos sin ventas o ventas bajas.": 1,
        "Con presencia en mercados electrónicos con ventas internacionales": 4,
    },
    "redes_sociales": {
        "Redes sociales inactivas o con bajo uso": 0,
        "Redes sociales activas y planificadas": 1,
        "Redes sociales activas y con generación de ventas": 2,
    },
}

BLOQUES: dict[str, list[str]] = {
    "Económico": ["situacion_empresa", "num_empleados", "facturacion"],
    "Internacional": [
        "experiencia_internacional",
        "alcance_actividad",
        "evolucion_facturacion",
        "involuccion_gerencia",
        "adaptacion_demanda",
        "adaptacion_producto",
        "num_paises",
    ],
    "Digitalización": [
        "tiene_web",
        "ecommerce",
        "mercados_electronicos",
        "redes_sociales",
    ],
}

BLOQUES_MAX: dict[str, int] = {
    "Económico": 15,
    "Internacional": 35,
    "Digitalización": 15,
}


def calculate_dpi_score(selections: dict) -> dict:
    """Calcula puntuación DPI por bloque y total. Devuelve scores y porcentajes."""
    scores: dict[str, int] = {}
    for bloque, criteria in BLOQUES.items():
        pts = 0
        for criterion in criteria:
            val = selections.get(criterion)
            if val:
                pts += DPI_SCORE_MAP.get(criterion, {}).get(val, 0)
        scores[bloque] = pts
    total = sum(scores.values())
    return {
        "scores": scores,
        "totals": BLOQUES_MAX,
        "total": total,
        "max_total": 65,
        "pct": round(total / 65 * 100),
    }


def generate_recommendations(selections: dict, score: dict) -> list[str]:
    """Devuelve lista de recomendaciones para criterios con puntuación baja."""
    _RECOS: dict[str, dict[str, str]] = {
        "experiencia_internacional": {
            "Ninguna experiencia": "Sin exportaciones previas — priorizar mercados cercanos (Portugal, Francia)",
            "Menos de 3 años": "Exportaciones incipientes — consolidar un mercado piloto antes de diversificar",
        },
        "ecommerce": {
            "Sin tienda web": "Sin tienda online — considerar Shopify o WooCommerce",
            "Tienda web propia con ventas bajas o irregulares": "Tienda con ventas bajas — optimizar SEO y experiencia de usuario",
        },
        "mercados_electronicos": {
            "Sin presencia en mercados electrónicos": "Sin presencia en marketplaces — explorar Etsy, Amazon Handmade",
            "Con presencia en mercados electrónicos sin ventas o ventas bajas.": "Presencia sin ventas — optimizar listings y política de precios",
        },
        "redes_sociales": {
            "Redes sociales inactivas o con bajo uso": "Sin actividad social — crear perfil Instagram mínimo viable",
        },
        "involuccion_gerencia": {
            "Sin participación alguna": "Gerencia sin implicación internacional — asignar responsable de exportación",
            "Escasamente involucrados": "Implicación baja de gerencia — establecer objetivos internacionales concretos",
        },
    }
    recos: list[str] = []
    for criterion, criterion_recos in _RECOS.items():
        val = selections.get(criterion)
        if val and val in criterion_recos:
            recos.append(criterion_recos[val])
    return recos


DRAFT_PROMPT = """Eres un experto en internacionalización empresarial. Basándote en el perfil DPI de la empresa, genera el análisis para el informe.

PERFIL DE LA EMPRESA:
{profile}

CONTEXTO SECTORIAL:
{context}

Genera ÚNICAMENTE JSON válido, sin markdown, con este formato exacto:
{{
  "dafo_debilidades": "texto con las debilidades detectadas (2-4 puntos)",
  "dafo_amenazas": "texto con las amenazas del entorno (2-4 puntos)",
  "dafo_fortalezas": "texto con las fortalezas identificadas (2-4 puntos)",
  "dafo_oportunidades": "texto con las oportunidades internacionales (2-4 puntos)",
  "definicion_potencial": "2-3 párrafos definiendo el potencial internacional de la empresa",
  "conclusiones": "1-2 párrafos con conclusiones y recomendaciones"
}}
"""


# --- Prefixes for stored fields ---
PREFIX_DIRECT = "dir_"
PREFIX_SELECTION = "sel_"
PREFIX_TEXT = "txt_"


def _log_to_jarvis(success: bool, document_id: int, notes: str = "") -> None:
    """Write pipeline result to JARVIS agent_actions table."""
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
        pass


def _run_claude(prompt: str) -> str:
    """Execute Claude headless."""
    result = subprocess.run(
        ["claude", "-p", prompt, "--model", settings.claude_model],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=settings.claude_timeout,
        cwd="/root/autoreporte",
    )
    return result.stdout.strip()


def _parse_json(raw: str) -> dict:
    """Parse JSON from Claude output with fallback."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end])
            except json.JSONDecodeError:
                pass
    return {}


def _load_fields_from_db(db: Session, document_id: int) -> dict[str, dict]:
    """Load all stored fields grouped by prefix."""
    fields = crud.get_fields(db, document_id)
    direct, selections, texts = {}, {}, {}
    for f in fields:
        if f.field_name.startswith(PREFIX_DIRECT):
            direct[f.field_name[len(PREFIX_DIRECT) :]] = f.field_value
        elif f.field_name.startswith(PREFIX_SELECTION):
            selections[f.field_name[len(PREFIX_SELECTION) :]] = f.field_value
        elif f.field_name.startswith(PREFIX_TEXT):
            texts[f.field_name[len(PREFIX_TEXT) :]] = f.field_value
    return {"direct_fields": direct, "selections": selections, "free_texts": texts}


# ─── Web search / confirmation ────────────────────────────────────────────────


async def trigger_web_search(db: Session, document_id: int) -> dict:
    """Search for the company website and store candidate.

    Returns a dict with status and the message to send to the consultant.
    """
    fields = crud.get_fields(db, document_id)
    empresa_name = next(
        (
            f.field_value
            for f in fields
            if f.field_name == f"{PREFIX_DIRECT}Razon_Social"
        ),
        "",
    )
    if not empresa_name:
        # No company name → skip web step
        return {"status": "no_company_name"}

    doc = db.get(Document, document_id)
    cache = None
    if doc and doc.web_search_cache:
        try:
            cache = json.loads(doc.web_search_cache)
        except Exception:
            pass

    result = find_best_candidate(empresa_name, cache=cache)

    # Persist search cache
    if doc:
        doc.web_search_cache = json.dumps(result["search_cache"])
        db.commit()

    # Persist DPI signals from web (only if confident enough)
    for key, value in result["dpi_signals"].get("selections", {}).items():
        conf = result["dpi_signals"]["confidence"].get(key, 0.0)
        crud.upsert_field(
            db,
            document_id,
            f"{PREFIX_SELECTION}{key}",
            value,
            confidence=conf,
            source="scraper",
        )
    for key, value in result["dpi_signals"].get("direct_fields", {}).items():
        crud.upsert_field(
            db,
            document_id,
            f"{PREFIX_DIRECT}{key}",
            value,
            confidence=0.9,
            source="scraper",
        )

    candidate_url = result["url"]
    if candidate_url:
        if doc:
            doc.web_candidate_url = candidate_url
            db.commit()
        crud.update_document_status(db, document_id, "waiting_web_confirmation")
        msg = (
            f"🌐 *He encontrado una posible web para {empresa_name}:*\n"
            f"{candidate_url}\n\n"
            "¿Es correcta?\n"
            "• Responde *CONFIRMAR* si es la web de la empresa\n"
            "• Responde *NO TIENE WEB* si la empresa no tiene página\n"
            "• Responde *OTRA WEB* y la URL si la web correcta es diferente"
        )
        return {
            "status": "waiting_web_confirmation",
            "message": msg,
            "url": candidate_url,
        }
    else:
        # No candidate found → ask consultant directly
        crud.update_document_status(db, document_id, "waiting_web_url")
        msg = (
            f"🌐 No encontré la web de *{empresa_name}* automáticamente.\n\n"
            "Por favor, envíame la URL de su página web (ej: https://empresa.com)\n"
            "o escribe *NO TIENE WEB* si no dispone de ella."
        )
        return {"status": "waiting_web_url", "message": msg}


async def handle_web_confirmation(
    db: Session, document_id: int, text: str, sender: str
) -> dict:
    """Process consultant reply to the web confirmation/url request.

    Returns dict with status and optional next message.
    """
    from channels.whatsapp import send_text

    text_lower = text.lower().strip()
    doc = db.get(Document, document_id)
    empresa_name = ""
    fields = crud.get_fields(db, document_id)
    for f in fields:
        if f.field_name == f"{PREFIX_DIRECT}Razon_Social":
            empresa_name = f.field_value or ""

    no_web_phrases = {"no tiene web", "sin web", "no hay web", "no tiene página"}
    is_no_web = any(p in text_lower for p in no_web_phrases)

    # Detect URL in text
    url_match = re.search(r"https?://[^\s]+", text)

    if is_no_web:
        # Consultant confirms no website
        crud.upsert_field(
            db,
            document_id,
            f"{PREFIX_SELECTION}tiene_web",
            "No",
            confidence=1.0,
            source="manual",
        )
        if doc:
            doc.web_candidate_url = None
            db.commit()
        crud.update_document_status(db, document_id, "processing")
        return {"status": "confirmed_no_web"}

    elif "confirmar" in text_lower and doc and doc.web_candidate_url:
        # Consultant confirms the suggested URL
        confirmed_url = doc.web_candidate_url
        crud.upsert_field(
            db,
            document_id,
            f"{PREFIX_SELECTION}tiene_web",
            "Sí",
            confidence=1.0,
            source="manual",
        )
        crud.upsert_field(
            db,
            document_id,
            f"{PREFIX_DIRECT}WEB",
            confirmed_url,
            confidence=1.0,
            source="manual",
        )
        crud.update_document_status(db, document_id, "processing")
        return {"status": "confirmed_url", "url": confirmed_url}

    elif url_match or "otra web" in text_lower:
        # Consultant provides a different URL or we need them to send one
        if url_match:
            new_url = url_match.group(0).rstrip(".,;)")
            crud.upsert_field(
                db,
                document_id,
                f"{PREFIX_SELECTION}tiene_web",
                "Sí",
                confidence=1.0,
                source="manual",
            )
            crud.upsert_field(
                db,
                document_id,
                f"{PREFIX_DIRECT}WEB",
                new_url,
                confidence=1.0,
                source="manual",
            )
            if doc:
                doc.web_candidate_url = new_url
                db.commit()
            crud.update_document_status(db, document_id, "processing")
            return {"status": "confirmed_url", "url": new_url}
        else:
            # "OTRA WEB" without URL → ask for it
            crud.update_document_status(db, document_id, "waiting_web_url")
            return {
                "status": "waiting_web_url",
                "message": "Perfecto, ¿cuál es la URL correcta? (ej: https://empresa.com)",
            }

    # Unrecognised reply — re-prompt
    return {"status": "unrecognised"}


# ─── FASE 1 ───────────────────────────────────────────────────────────────────


async def process_document(
    db: Session,
    document_id: int,
    file_path: Path,
    transcript_text: str = "",
) -> dict:
    """FASE 1: Read document → extract DPI fields → save to DB → trigger FASE 2.

    Args:
        transcript_text: Optional pre-extracted transcript (CAMBIO 2).

    Returns:
        dict with status, null_selections, question_message
    """
    crud.update_document_status(db, document_id, "processing")

    try:
        text = read_document_text(file_path)
        if not text:
            crud.update_document_status(db, document_id, "error")
            _log_to_jarvis(False, document_id, "empty_text")
            return {
                "status": "error",
                "reason": "No se pudo extraer texto del documento.",
            }

        extracted = extract_dpi_fields(text, transcript_text)  # CAMBIO 2

        # Persist direct fields
        for key, value in extracted.get("direct_fields", {}).items():
            crud.upsert_field(
                db,
                document_id,
                f"{PREFIX_DIRECT}{key}",
                value,
                confidence=0.9,
                source="claude",
            )

        # Compute VALOR_CONSTITUCION (seniority in years) from año_inicio
        año_inicio_raw = extracted.get("direct_fields", {}).get("año_inicio")
        if año_inicio_raw:
            try:
                from datetime import datetime as _dt

                years_active = _dt.now().year - int(str(año_inicio_raw).strip())
                if 0 <= years_active <= 200:
                    valor = f"{years_active} año{'s' if years_active != 1 else ''}"
                    crud.upsert_field(
                        db,
                        document_id,
                        f"{PREFIX_DIRECT}VALOR_CONSTITUCION",
                        valor,
                        confidence=0.95,
                        source="calculated",
                    )
            except (ValueError, TypeError):
                pass

        # Persist selections with confidence
        confidence_map = extracted.get("confidence", {})
        for key, value in extracted.get("selections", {}).items():
            crud.upsert_field(
                db,
                document_id,
                f"{PREFIX_SELECTION}{key}",
                value,
                confidence=float(confidence_map.get(key, 0.0)),
                source="claude",
            )

        # Web search step: runs BEFORE asking the consultant questions
        web_result = await trigger_web_search(db, document_id)
        if web_result["status"] in ("waiting_web_confirmation", "waiting_web_url"):
            _log_to_jarvis(True, document_id, f"fase1_ok web={web_result['status']}")
            return {
                "status": web_result["status"],
                "question_message": web_result["message"],
            }

        # Generate user questions for null selections
        question_msg = generate_questions(db, document_id)
        null_count = question_msg.count("\n•") if question_msg else 0

        if null_count == 0:
            # All selections filled → jump straight to FASE 3
            return await generate_draft_texts(db, document_id)

        crud.update_document_status(db, document_id, "waiting_user_response")
        _log_to_jarvis(True, document_id, f"fase1_ok null_selections={null_count}")
        return {
            "status": "waiting_user_response",
            "question_message": question_msg,
        }

    except Exception as exc:
        crud.update_document_status(db, document_id, "error")
        _log_to_jarvis(False, document_id, str(exc)[:200])
        raise


async def proceed_after_web(db: Session, document_id: int) -> dict:
    """Called after web confirmation is resolved. Continues to FASE 2 or FASE 3."""
    question_msg = generate_questions(db, document_id)
    null_count = question_msg.count("\n•") if question_msg else 0
    if null_count == 0:
        return await generate_draft_texts(db, document_id)
    crud.update_document_status(db, document_id, "waiting_user_response")
    return {"status": "waiting_user_response", "question_message": question_msg}


# ─── FASE 2 ───────────────────────────────────────────────────────────────────


def generate_questions(db: Session, document_id: int) -> str:
    """FASE 2: Build question message for criteria that Claude couldn't determine.

    Returns:
        Human-readable message string, or empty string if no questions needed.
    """
    fields = crud.get_fields(db, document_id)
    null_criteria = [
        f.field_name[len(PREFIX_SELECTION) :]
        for f in fields
        if f.field_name.startswith(PREFIX_SELECTION) and f.field_value is None
    ]

    if not null_criteria:
        return ""

    # MEJORA 6: load empresa name and sector for contextual questions
    empresa_name = ""
    sector = ""
    for f in fields:
        if f.field_name == f"{PREFIX_DIRECT}Razon_Social":
            empresa_name = f.field_value or ""
        elif f.field_name == f"{PREFIX_DIRECT}sector":
            sector = f.field_value or ""

    total = len(CRITERION_OPTIONS)
    completados = total - len(null_criteria)
    _num_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    # Build context prefix for each question block
    empresa_ctx = ""
    if empresa_name:
        empresa_ctx = f"*{empresa_name}*"
        if sector:
            empresa_ctx += f" ({sector})"

    # Batch: only show first QUESTION_BATCH_SIZE criteria per message
    batch = null_criteria[:QUESTION_BATCH_SIZE]
    remaining_count = len(null_criteria) - len(batch)

    blocks = []
    for i, key in enumerate(batch, 1):
        raw = CRITERION_QUESTIONS.get(key, f"Criterio: {key}")
        if "\n   → " in raw:
            question_text, options_str = raw.split("\n   → ", 1)
            options = [o.strip() for o in options_str.split(" / ")]
        else:
            question_text = raw
            options = CRITERION_OPTIONS.get(key, [])

        option_lines = "\n".join(
            f"  {_num_emojis[j] if j < len(_num_emojis) else f'{j + 1}.'} {opt}"
            for j, opt in enumerate(options)
        )
        # MEJORA 6: include empresa/sector context before question
        header = f"Para {empresa_ctx}:\n" if empresa_ctx else ""
        blocks.append(f"{header}*{i}. {question_text}*\n{option_lines}")

    questions_text = "\n\n".join(blocks)
    more_note = (
        f"\n\n_({remaining_count} pregunta{'s' if remaining_count != 1 else ''} más en el siguiente mensaje)_"
        if remaining_count > 0
        else ""
    )

    return (
        f"🔍 *ANÁLISIS COMPLETADO*\n\n"
        f"He podido completar automáticamente *{completados} de "
        f"{total} criterios* del informe.\n\n"
        f"Necesito que respondas estas preguntas "
        f"({len(batch)} de {len(null_criteria)} pendientes):\n\n"
        f"{questions_text}\n\n"
        f"{'─' * 30}\n"
        f"💡 _Responde con el número de la opción elegida_\n"
        f"_Ej: '1' para la primera opción_"
        f"{more_note}"
    )


# ─── FASE 2b: Parse WhatsApp text reply ───────────────────────────────────────


async def process_user_response(db: Session, document_id: int, text: str) -> dict:
    """Parse a free-text WhatsApp answer and store selections.

    Expects numbered lines like "1. Más de 2 años\\n2. Más de 2".
    Falls back to treating the whole text as a single answer if no numbers found.

    Returns:
        dict with status='waiting_user_response' (more questions remain)
        or the result of generate_draft_texts if all criteria are filled.
    """
    fields = crud.get_fields(db, document_id)
    null_criteria = [
        f.field_name[len(PREFIX_SELECTION) :]
        for f in fields
        if f.field_name.startswith(PREFIX_SELECTION) and f.field_value is None
    ]

    if not null_criteria:
        return await generate_draft_texts(db, document_id)

    # Parse numbered answers ("1. answer", "1) answer", "• 1. answer")
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    answers: list[str] = []
    for line in lines:
        match = re.match(r"^[•\-]?\s*\d+[.)]\s*(.+)$", line)
        if match:
            answers.append(match.group(1).strip())

    # Fallback: single answer for a single pending question
    if not answers and len(null_criteria) == 1:
        answers = [text.strip()]

    for i, criterion in enumerate(null_criteria):
        if i >= len(answers):
            break
        raw_answer = answers[i]
        options = CRITERION_OPTIONS.get(criterion, [])
        matched = next(
            (
                opt
                for opt in options
                if opt.lower() in raw_answer.lower()
                or raw_answer.lower() in opt.lower()
            ),
            raw_answer,
        )
        crud.upsert_field(
            db,
            document_id,
            f"{PREFIX_SELECTION}{criterion}",
            matched,
            confidence=1.0,
            source="whatsapp",
        )

    remaining = generate_questions(db, document_id)
    if remaining:
        return {"status": "waiting_user_response", "question_message": remaining}

    return await generate_draft_texts(db, document_id)


# ─── FASE 3 ───────────────────────────────────────────────────────────────────


async def generate_draft_texts(db: Session, document_id: int) -> dict:
    """FASE 3: Generate DAFO + free texts using Claude, send draft for approval.

    Returns:
        dict with status='waiting_approval', draft_message
    """
    stored = _load_fields_from_db(db, document_id)
    selections = stored["selections"]
    direct = stored["direct_fields"]

    # Build human-readable profile for Claude
    profile_lines = [f"Empresa: {direct.get('Razon_Social', 'N/D')}"]
    for key, value in selections.items():
        profile_lines.append(f"{key}: {value or 'No especificado'}")
    profile = "\n".join(profile_lines)

    # MEJORA 4: include sector and producto_servicio in DAFO context
    context_parts = []
    if direct.get("sector"):
        context_parts.append(f"Sector: {direct['sector']}")
    if direct.get("producto_servicio"):
        context_parts.append(
            f"Producto/servicio exportable: {direct['producto_servicio']}"
        )
    context = "\n".join(context_parts) if context_parts else "No especificado"

    raw = _run_claude(DRAFT_PROMPT.format(profile=profile, context=context))
    draft = _parse_json(raw)

    if not draft:
        crud.update_document_status(db, document_id, "error")
        return {"status": "error", "reason": "Claude no pudo generar el borrador."}

    # Persist free texts
    for key, value in draft.items():
        crud.upsert_field(
            db,
            document_id,
            f"{PREFIX_TEXT}{key}",
            value,
            confidence=0.8,
            source="claude",
        )

    crud.update_document_status(db, document_id, "waiting_approval")
    _log_to_jarvis(True, document_id, "fase3_draft_ready")

    # CAMBIO 5: compact score summary instead of verbose DAFO preview
    empresa = direct.get("Razon_Social", "Empresa")
    score = calculate_dpi_score(selections)
    recos = generate_recommendations(selections, score)

    s = score["scores"]
    t = score["totals"]
    recos_text = (
        "\n".join(f"• {r}" for r in recos)
        if recos
        else "• Sin áreas críticas detectadas"
    )
    draft_preview = (
        f"📊 *Diagnóstico DPI — {empresa}*\n\n"
        f"🏆 *Puntuación: {score['total']}/{score['max_total']} pts* ({score['pct']}%)\n\n"
        f"📦 Económico: {s['Económico']}/{t['Económico']} | "
        f"🌍 Internacional: {s['Internacional']}/{t['Internacional']} | "
        f"💻 Digitalización: {s['Digitalización']}/{t['Digitalización']}\n\n"
        f"⚠️ *Áreas de mejora:*\n{recos_text}\n\n"
        f"¿Generamos el informe? Responde *APRUEBO*"
    )
    return {"status": "waiting_approval", "draft_message": draft_preview}


# ─── FASE 4 ───────────────────────────────────────────────────────────────────


def generate_final_docx(db: Session, document_id: int) -> dict:
    """FASE 4: Render final DOCX and mark document as complete.

    Returns:
        dict with status='complete', output_path
    """
    stored = _load_fields_from_db(db, document_id)
    empresa_name = stored["direct_fields"].get("Razon_Social", str(document_id))

    output_path = render_template(
        document_id=document_id,
        data=stored,
        empresa_name=empresa_name,
    )

    missing_sel = [k for k, v in stored["selections"].items() if not v]
    crud.create_or_update_generated_docx(
        db,
        document_id=document_id,
        output_path=str(output_path),
        fields_complete=len(missing_sel) == 0,
        missing_fields=missing_sel,
    )
    crud.update_document_status(db, document_id, "complete")
    _log_to_jarvis(True, document_id, f"fase4_complete path={output_path.name}")

    return {
        "status": "complete",
        "output_path": str(output_path),
        "empresa_name": empresa_name,
    }


async def apply_draft_changes(db: Session, document_id: int, changes_text: str) -> dict:
    """Re-run FASE 3 incorporating user-requested changes to the draft.

    Args:
        changes_text: User's free-text description of what to change.

    Returns:
        dict with status='waiting_approval', new draft_message
    """
    stored = _load_fields_from_db(db, document_id)
    current_texts = stored.get("free_texts", {})

    revision_prompt = (
        f"El usuario ha solicitado los siguientes cambios al borrador:\n{changes_text}\n\n"
        f"Borrador actual:\n{json.dumps(current_texts, ensure_ascii=False, indent=2)}\n\n"
        "Genera el borrador REVISADO con los cambios aplicados. "
        "Responde ÚNICAMENTE con JSON con las mismas claves que el borrador actual."
    )
    raw = _run_claude(revision_prompt)
    revised = _parse_json(raw)

    if not revised:
        return {"status": "error", "reason": "Claude no pudo aplicar los cambios."}

    for key, value in revised.items():
        crud.upsert_field(
            db,
            document_id,
            f"{PREFIX_TEXT}{key}",
            value,
            confidence=0.9,
            source="claude",
        )

    stored_updated = _load_fields_from_db(db, document_id)
    empresa = stored_updated["direct_fields"].get("Razon_Social", "")
    draft_preview = (
        f"Borrador revisado para {empresa}.\n\n"
        f"DAFO — FORTALEZAS:\n{revised.get('dafo_fortalezas', '')}\n\n"
        f"DEFINICIÓN DEL POTENCIAL:\n{revised.get('definicion_potencial', '')[:300]}...\n\n"
        "¿Apruebas ahora el borrador? Responde 'Apruebo' o indica nuevos cambios."
    )
    return {"status": "waiting_approval", "draft_message": draft_preview}
