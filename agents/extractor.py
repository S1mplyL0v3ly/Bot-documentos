"""Field extractor for DPI Canarias documents using Claude headless."""

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import settings

CURRENT_YEAR = datetime.now().year

# ─── Normalization maps ────────────────────────────────────────────────────────

# MEJORA 1: Map free-form facturación text → exact DPI option
FACTURACION_MAP: dict[str, str] = {
    "menos de 250.000": "Menos de 200.000 €",
    "menos de 200.000": "Menos de 200.000 €",
    "entre 250.000 y 500.000": "Entre 200.000 y 500.000 €",
    "entre 250.000 € y 500.000": "Entre 200.000 y 500.000 €",
    "entre 200.000 y 500.000": "Entre 200.000 y 500.000 €",
    "entre 500.000 y 1": "Entre 500.000 y 1.000.000 €",
    "entre 500.001 y 1": "Entre 500.000 y 1.000.000 €",
    "entre 1 y 5": "Más de 1.000.000 €",
    "más de 5 millones": "Más de 1.000.000 €",
    "más de 1.000.000": "Más de 1.000.000 €",
}

# MEJORA 2: Map free-form experiencia text → exact DPI option
EXPERIENCIA_MAP: dict[str, str] = {
    "no hemos exportado nunca": "Ninguna",
    "ninguna experiencia": "Ninguna",
    "nunca hemos exportado": "Ninguna",
    "hemos exportado de forma puntual": "Menos de 3 años",
    "exportamos de manera regular": "Más de 5 años",
    "contamos con un departamento": "Más de 5 años",
    "menos de 3 años": "Menos de 3 años",
    "más de 5 años": "Más de 5 años",
}

# Opciones válidas por criterio — usadas para validar la respuesta de Claude
CRITERION_OPTIONS: dict[str, list[str]] = {
    "situacion_empresa": ["No constituida", "Menos de 2 años", "Más de 2 años"],
    "num_empleados": ["Menos de 2", "Más de 2"],
    "facturacion": [
        "Menos de 200.000 €",
        "Entre 200.000 y 500.000 €",
        "Entre 500.000 y 1.000.000 €",
        "Más de 1.000.000 €",
    ],
    "evolucion_facturacion": ["En decrecimiento", "Estable", "En crecimiento"],
    "recursos_internacionalizacion": ["No", "Sí"],
    "experiencia_internacional": ["Ninguna", "Menos de 3 años", "Más de 5 años"],
    "alcance_actividad": ["Insular", "Nacional", "Internacional"],
    "num_paises": ["Ninguno", "De 1 a 5", "Más de 5"],
    "personal_dedicado": ["No", "Sí"],
    "involuccion_gerencia": [
        "Sin participación",
        "Escasamente involucrados",
        "Medianamente involucrados",
        "Directamente involucrados",
    ],
    "adaptacion_demanda": ["Baja", "Media", "Alta"],
    "adaptacion_producto": ["Baja", "Media", "Alta"],
    "tiene_web": ["No", "Sí"],
    "ecommerce": [
        "Sin tienda web propia",
        "Tienda web propia con ventas bajas",
        "Tienda web propia con ventas regulares a nivel nacional",
        "Tienda web propia con ventas regulares a nivel internacional",
    ],
    "mercados_electronicos": [
        "Sin presencia en mercados electrónicos",
        "Con presencia pero sin ventas",
        "Con ventas nacionales",
        "Con ventas internacionales",
    ],
    "redes_sociales": [
        "Redes sociales inactivas o inexistentes",
        "Redes sociales activas y planificadas",
        "Redes sociales que generan ventas",
    ],
}

EXTRACTOR_PROMPT = """Analiza el siguiente documento de empresa y extrae dos grupos de información.

GRUPO 1 — DATOS DIRECTOS (si aparecen en el documento):
- Razon_Social: nombre legal de la empresa
- CIF: identificador fiscal
- WEB: URL exacta de la página web (null si no aparece la URL, aunque se mencione que tienen web)
- Persona_Contacto: nombre de la persona de contacto
- Cargo: cargo de la persona de contacto
- email: correo electrónico de contacto
- Telefono_Contacto: teléfono
- sector: sector o industria de la empresa (ej: "Joyería y bisutería artesanal")
- producto_servicio: producto o servicio exportable principal (ej: "Joyería artesanal de plata")
- año_inicio: año de inicio de actividad (sólo el número, ej: 2018)

GRUPO 2 — CRITERIOS DPI (elige la opción MÁS ADECUADA para cada criterio, o null si no hay información suficiente):

situacion_empresa → opciones: "No constituida" | "Menos de 2 años" | "Más de 2 años"
num_empleados → opciones: "Menos de 2" | "Más de 2"
facturacion → opciones: "Menos de 200.000 €" | "Entre 200.000 y 500.000 €" | "Entre 500.000 y 1.000.000 €" | "Más de 1.000.000 €"
evolucion_facturacion → opciones: "En decrecimiento" | "Estable" | "En crecimiento"
recursos_internacionalizacion → opciones: "No" | "Sí"
experiencia_internacional → opciones: "Ninguna" | "Menos de 3 años" | "Más de 5 años"
alcance_actividad → opciones: "Insular" | "Nacional" | "Internacional"
num_paises → opciones: "Ninguno" | "De 1 a 5" | "Más de 5"
personal_dedicado → opciones: "No" | "Sí"
involuccion_gerencia → opciones: "Sin participación" | "Escasamente involucrados" | "Medianamente involucrados" | "Directamente involucrados"
adaptacion_demanda → opciones: "Baja" | "Media" | "Alta"
adaptacion_producto → opciones: "Baja" | "Media" | "Alta"
tiene_web → opciones: "No" | "Sí"  (si hay WEB en datos directos O el documento indica que tienen web → "Sí"; WEB puede estar vacío)
ecommerce → opciones: "Sin tienda web propia" | "Tienda web propia con ventas bajas" | "Tienda web propia con ventas regulares a nivel nacional" | "Tienda web propia con ventas regulares a nivel internacional"
mercados_electronicos → opciones: "Sin presencia en mercados electrónicos" | "Con presencia pero sin ventas" | "Con ventas nacionales" | "Con ventas internacionales"
redes_sociales → opciones: "Redes sociales inactivas o inexistentes" | "Redes sociales activas y planificadas" | "Redes sociales que generan ventas"

INSTRUCCIONES:
- Usa null si la información no aparece o no es suficiente para elegir con confianza ≥ 0.7.
- La confianza refleja qué tan seguro estás: 0.0 = adivinanza, 1.0 = dato explícito en el documento.
- Responde ÚNICAMENTE con JSON válido, sin markdown.

Formato exacto de respuesta:
{{
  "direct_fields": {{
    "Razon_Social": "valor o null",
    "CIF": "valor o null",
    "WEB": "URL exacta o null",
    "Persona_Contacto": "valor o null",
    "Cargo": "valor o null",
    "email": "valor o null",
    "Telefono_Contacto": "valor o null",
    "sector": "valor o null",
    "producto_servicio": "valor o null",
    "año_inicio": "valor numérico o null"
  }},
  "selections": {{
    "situacion_empresa": "opción exacta o null",
    "num_empleados": "opción exacta o null",
    "facturacion": "opción exacta o null",
    "evolucion_facturacion": "opción exacta o null",
    "recursos_internacionalizacion": "opción exacta o null",
    "experiencia_internacional": "opción exacta o null",
    "alcance_actividad": "opción exacta o null",
    "num_paises": "opción exacta o null",
    "personal_dedicado": "opción exacta o null",
    "involuccion_gerencia": "opción exacta o null",
    "adaptacion_demanda": "opción exacta o null",
    "adaptacion_producto": "opción exacta o null",
    "tiene_web": "opción exacta o null",
    "ecommerce": "opción exacta o null",
    "mercados_electronicos": "opción exacta o null",
    "redes_sociales": "opción exacta o null"
  }},
  "confidence": {{
    "situacion_empresa": 0.0,
    "num_empleados": 0.0,
    "facturacion": 0.0,
    "evolucion_facturacion": 0.0,
    "recursos_internacionalizacion": 0.0,
    "experiencia_internacional": 0.0,
    "alcance_actividad": 0.0,
    "num_paises": 0.0,
    "personal_dedicado": 0.0,
    "involuccion_gerencia": 0.0,
    "adaptacion_demanda": 0.0,
    "adaptacion_producto": 0.0,
    "tiene_web": 0.0,
    "ecommerce": 0.0,
    "mercados_electronicos": 0.0,
    "redes_sociales": 0.0
  }}
}}

DOCUMENTO:
{text}
"""

CONFIDENCE_THRESHOLD = 0.7


def run_claude(prompt: str) -> str:
    """Execute Claude headless and return stdout."""
    result = subprocess.run(
        ["claude", "-p", prompt, "--model", settings.claude_model],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=settings.claude_timeout,
        cwd="/root/autoreporte",
    )
    return result.stdout.strip()


def read_document_text(file_path: Path) -> str:
    """Extract raw text from a document file.

    Supports: .pdf, .docx, .txt, .png, .jpg, .jpeg
    """
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        import pdfplumber

        with pdfplumber.open(file_path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)

    if suffix == ".docx":
        from docx import Document

        doc = Document(str(file_path))
        return "\n".join(p.text for p in doc.paragraphs)

    if suffix in {".txt", ".md", ".csv"}:
        return file_path.read_text(encoding="utf-8")

    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        # Claude vision: pass path as special marker, orchestrator handles multimodal
        return f"[IMAGE_FILE:{file_path.as_posix()}]"

    return ""


def _parse_json_response(raw: str) -> dict:
    """Try to parse JSON from Claude response, with fallback extraction."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end])
            except json.JSONDecodeError:
                pass
    return {}


def _null_low_confidence(data: dict) -> dict:
    """Set selections to null when confidence < CONFIDENCE_THRESHOLD.

    Args:
        data: Parsed response from Claude with direct_fields, selections, confidence.

    Returns:
        Same dict with low-confidence selections nulled out.
    """
    selections = data.get("selections", {})
    confidence = data.get("confidence", {})
    nulled = dict(selections)
    for key, value in selections.items():
        if value is not None and float(confidence.get(key, 0.0)) < CONFIDENCE_THRESHOLD:
            nulled[key] = None
    data["selections"] = nulled
    return data


def _apply_value_map(value: str, mapping: dict[str, str]) -> str:
    """Return the normalized value if any mapping key is a substring of value."""
    v_lower = value.strip().lower()
    for key, normalized in mapping.items():
        if key in v_lower:
            return normalized
    return value


# MEJORA 3: Patterns to extract year of incorporation from document text
_YEAR_PATTERNS = [
    r"año de inicio de (?:la )?actividad[^:\n]*[:\s]+(\d{4})",
    r"año en que la empresa comenzó[^:\n]*[:\s]+(\d{4})",
    r"inicio de actividad[^:\n]*[:\s]+(\d{4})",
    r"fundad[ao] en\s+(\d{4})",
    r"constituida? en\s+(\d{4})",
    r"creada? en\s+(\d{4})",
    r"año de constitución[^:\n]*[:\s]+(\d{4})",
]


def _deduce_situacion_from_year(text: str) -> tuple[str | None, float]:
    """Extract year of incorporation from text and map to situacion_empresa."""
    for pattern in _YEAR_PATTERNS:
        m = re.search(pattern, text.lower())
        if m:
            year = int(m.group(1))
            years_active = CURRENT_YEAR - year
            if years_active < 0 or years_active > 200:
                continue
            if years_active < 2:
                return "Menos de 2 años", 0.95
            return "Más de 2 años", 0.95
    return None, 0.0


def _normalize_selections(data: dict, text: str) -> dict:
    """Post-process Claude's output to normalize and infer selection values.

    MEJORA 1: Normalize facturación using FACTURACION_MAP.
    MEJORA 2: Normalize experiencia_internacional using EXPERIENCIA_MAP.
    MEJORA 3: Infer situacion_empresa from año_inicio when null.
    MEJORA 5: Set tiene_web="Sí" when WEB url is present in direct_fields.
    """
    selections = data.get("selections", {})
    confidence = data.get("confidence", {})
    direct = data.get("direct_fields", {})

    # MEJORA 1: normalize facturación
    fac = selections.get("facturacion")
    if fac and fac not in CRITERION_OPTIONS["facturacion"]:
        selections["facturacion"] = _apply_value_map(fac, FACTURACION_MAP)

    # MEJORA 2: normalize experiencia_internacional
    exp = selections.get("experiencia_internacional")
    if exp and exp not in CRITERION_OPTIONS["experiencia_internacional"]:
        selections["experiencia_internacional"] = _apply_value_map(exp, EXPERIENCIA_MAP)

    # MEJORA 3: infer situacion_empresa from year if still null
    if not selections.get("situacion_empresa"):
        # Try from Claude's extracted año_inicio first
        año_inicio = direct.get("año_inicio")
        if año_inicio:
            try:
                year = int(str(año_inicio).strip())
                years_active = CURRENT_YEAR - year
                if 0 <= years_active <= 200:
                    sel = "Menos de 2 años" if years_active < 2 else "Más de 2 años"
                    selections["situacion_empresa"] = sel
                    confidence["situacion_empresa"] = 0.95
            except (ValueError, TypeError):
                pass
        # Fallback: regex scan on raw text
        if not selections.get("situacion_empresa"):
            inferred, conf = _deduce_situacion_from_year(text)
            if inferred:
                selections["situacion_empresa"] = inferred
                confidence["situacion_empresa"] = conf

    # MEJORA 5: WEB url present → tiene_web is "Sí" regardless
    if direct.get("WEB"):
        if not selections.get("tiene_web"):
            selections["tiene_web"] = "Sí"
            confidence["tiene_web"] = 1.0

    data["selections"] = selections
    data["confidence"] = confidence
    return data


def extract_dpi_fields(text: str) -> dict:
    """Extract DPI fields and selections from document text using Claude.

    Args:
        text: Raw text from the document (up to 6000 chars).

    Returns:
        dict with keys: direct_fields, selections, confidence
        Selections with confidence < 0.7 are set to null.
    """
    prompt = EXTRACTOR_PROMPT.format(text=text[:6000])
    raw = run_claude(prompt)
    data = _parse_json_response(raw)

    if not data:
        return {
            "direct_fields": {},
            "selections": {k: None for k in CRITERION_OPTIONS},
            "confidence": {k: 0.0 for k in CRITERION_OPTIONS},
        }

    data = _normalize_selections(data, text)
    return _null_low_confidence(data)
