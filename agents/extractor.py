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
    "sin experiencia": "Ninguna",
    "no exportamos": "Ninguna",
    "hemos exportado de forma puntual": "Menos de 3 años",
    "exportaciones puntuales": "Menos de 3 años",
    "ventas puntuales": "Menos de 3 años",
    "de forma esporádica": "Menos de 3 años",
    "exportamos de manera regular": "Más de 5 años",
    "contamos con un departamento": "Más de 5 años",
    "menos de 3 años": "Menos de 3 años",
    "más de 5 años": "Más de 5 años",
}

# MEJORA 7: Map free-form involucción gerencia text → exact DPI option
INVOLUCCION_MAP: dict[str, str] = {
    "directamente involucrad": "Directamente involucrados",
    "directamente implicad": "Directamente involucrados",
    "totalmente involucrad": "Directamente involucrados",
    "medianamente involucrad": "Medianamente involucrados",
    "medianamente implicad": "Medianamente involucrados",
    "escasamente involucrad": "Escasamente involucrados",
    "escasamente implicad": "Escasamente involucrados",
    "sin participación": "Sin participación",
    "no participa": "Sin participación",
}

# MEJORA 7: Map free-form alcance text → exact DPI option
ALCANCE_MAP: dict[str, str] = {
    "comunidad europea": "Internacional",
    "unión europea": "Internacional",
    "mercado europeo": "Internacional",
    "mercado internacional": "Internacional",
    "exportamos a": "Internacional",
    "ventas internacionales": "Internacional",
    "america": "Internacional",
    "estados unidos": "Internacional",
    "reino unido": "Internacional",
    "nacional": "Nacional",
    "mercado nacional": "Nacional",
    "península": "Nacional",
    "solo españa": "Nacional",
    "insular": "Insular",
    "solo canarias": "Insular",
    "mercado canario": "Insular",
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

El documento puede ser un cuestionario DPI (Diagnóstico de Potencial de Internacionalización)
del programa Canarias Expande / Cámara de Comercio. En ese caso, las respuestas aparecen
directamente tras cada pregunta. Lee la respuesta dada, NO la pregunta.

EJEMPLOS de lectura del cuestionario Canarias Expande:
• "Año de inicio de la actividad empresarial: 2020" → año_inicio = 2020
• "Número de empleados: 4 personas a tiempo completo" → num_empleados = "Más de 2"
• "Facturación total durante el ejercicio 2024: Menos de 250.000 €" → facturacion (aproximar al rango DPI)
• "Evolución de la facturación en los últimos 3 años: En crecimiento" → evolucion_facturacion = "En crecimiento"
• "¿Dispone de recursos económicos y humanos para un plan de internacionalización? Sí, contamos..." → recursos_internacionalizacion = "Sí"
• "Experiencia exportadora: Hemos exportado de forma puntual..." → experiencia_internacional = "Menos de 3 años"
• "Alcance actual de la actividad comercial: Nacional" → alcance_actividad = "Nacional"
• "Número de países donde vende regularmente: Ninguno" → num_paises = "Ninguno"
• "¿Tiene personal dedicado exclusivamente a comercio exterior? No" → personal_dedicado = "No"
• "Implicación de la gerencia: La fundadora está Directamente involucrada..." → involuccion_gerencia = "Directamente involucrados"
• "Grado de adaptación de la oferta a la demanda internacional: Media" → adaptacion_demanda = "Media"
• "Grado de adaptación del producto al mercado internacional: Alta" → adaptacion_producto = "Alta"
• "¿Dispone de página web corporativa? Sí tiene web..." → tiene_web = "Sí" (WEB = null si no da la URL)
• "Tienda online: Sin tienda web propia actualmente" → ecommerce = "Sin tienda web propia"
• "Presencia en mercados electrónicos: Con presencia pero sin ventas regulares" → mercados_electronicos = "Con presencia pero sin ventas"
• "Actividad en redes sociales: Redes sociales activas y planificadas" → redes_sociales = "Redes sociales activas y planificadas"

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
- Las líneas que empiezan por ◉ o ☑ en la sección OPCIONES SELECCIONADAS son selecciones confirmadas
  visualmente. Para esas opciones, usa confianza = 0.95 aunque la opción del formulario no coincida
  exactamente con las opciones DPI listadas (aproxima al rango más cercano con confianza 0.95).
- Si el documento es un cuestionario con respuestas explícitas, usa confianza ≥ 0.95 para esas respuestas.
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


_GOOGLE_FORMS_ORANGE = (0.702, 0.4275, 0)  # stroke color of selected radio button


def _is_orange(color: object) -> bool:
    """Return True if color matches Google Forms' selection orange."""
    if not color or not isinstance(color, (list, tuple)) or len(color) < 3:
        return False
    return all(abs(a - b) < 0.05 for a, b in zip(color, _GOOGLE_FORMS_ORANGE))


def read_google_forms_pdf(pdf_path: str) -> dict:
    """Read a Google Forms PDF and detect selected checkboxes and radio buttons.

    Patterns detected via curve geometry (verified on Canarias Expande 2025 PDFs):
    - Radio button selected: curve pts=18, stroke=True, fill=False,
      non_stroking_color=(0.702, 0.4275, 0) — orange inner dot
    - Checkbox selected: curve pts=5, fill=True — tick mark
      (deduplicated by y position, as tick pairs share the same y)

    Returns:
        dict with 'selected_options': list of {text, type, y}
    """
    import pdfplumber

    selected_options = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            curves = page.curves
            page_height = page.height

            # Radio buttons — pts=18 with orange stroke color (not fill)
            for curve in curves:
                if (
                    len(curve["pts"]) == 18
                    and curve.get("stroke") is True
                    and _is_orange(curve.get("non_stroking_color"))
                ):
                    y_pos = page_height - curve["y0"]
                    near_words = [w for w in words if abs(w["top"] - y_pos) < 15]
                    if near_words:
                        text = " ".join(w["text"] for w in near_words)
                        selected_options.append(
                            {"text": text, "type": "radio", "y": y_pos}
                        )

            # Checkboxes — tick mark has 5 points; deduplicate by y position
            checkbox_ys: set[float] = set()
            for curve in curves:
                if len(curve["pts"]) == 5 and curve.get("fill") is True:
                    y_rounded = round(curve["y0"], 0)
                    if y_rounded not in checkbox_ys:
                        checkbox_ys.add(y_rounded)
                        y_pos = page_height - curve["y0"]
                        near_words = [w for w in words if abs(w["top"] - y_pos) < 15]
                        if near_words:
                            text = " ".join(w["text"] for w in near_words)
                            selected_options.append(
                                {"text": text, "type": "checkbox", "y": y_pos}
                            )

    return {"selected_options": selected_options}


def read_document_text(file_path: Path) -> str:
    """Extract raw text from a document file.

    Supports: .pdf, .docx, .txt, .png, .jpg, .jpeg
    For PDFs: uses visual checkbox/radio detection first, then appends full text.
    """
    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        import pdfplumber

        forms_data = read_google_forms_pdf(str(file_path))
        selected = forms_data["selected_options"]

        lines = ["=== OPCIONES SELECCIONADAS EN EL FORMULARIO ===\n"]
        for opt in selected:
            marker = "☑" if opt["type"] == "checkbox" else "◉"
            lines.append(f"{marker} {opt['text']}")

        with pdfplumber.open(str(file_path)) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

        lines.append("\n=== TEXTO COMPLETO DEL FORMULARIO ===\n")
        lines.append(full_text)

        return "\n".join(lines)

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


def _boost_visual_confidence(data: dict, cuestionario_text: str) -> dict:
    """MEJORA 8: Fill selections and set confidence=1.0 for values from visual detections.

    Visual selections (◉/☑ markers in '=== OPCIONES SELECCIONADAS ===') are 100%
    reliable. Claude may return low confidence when cuestionario options don't exactly
    match DPI options (e.g. 'Menos de 250.000 €' vs 'Menos de 200.000 €'). This
    step applies normalization maps directly to the visual section, bypassing the
    confidence threshold for confirmed visual detections.

    Called AFTER _normalize_selections and BEFORE _null_low_confidence.
    """
    if "=== OPCIONES SELECCIONADAS" not in cuestionario_text:
        return data

    start = cuestionario_text.index("=== OPCIONES SELECCIONADAS")
    end = (
        cuestionario_text.index("=== TEXTO COMPLETO")
        if "=== TEXTO COMPLETO" in cuestionario_text
        else len(cuestionario_text)
    )
    visual_lower = cuestionario_text[start:end].lower()

    selections = data.get("selections", {})
    confidence = data.get("confidence", {})

    # (normalization_map, criterion_key) — first matching key wins per criterion
    visual_maps = [
        (FACTURACION_MAP, "facturacion"),
        (EXPERIENCIA_MAP, "experiencia_internacional"),
        (INVOLUCCION_MAP, "involuccion_gerencia"),
        (ALCANCE_MAP, "alcance_actividad"),
    ]
    for map_dict, key in visual_maps:
        for pattern, value in map_dict.items():
            if pattern in visual_lower:
                if not selections.get(key):
                    # Claude returned null — fill from visual detection
                    selections[key] = value
                    confidence[key] = 1.0
                elif selections.get(key) == value:
                    # Claude returned correct value but low confidence — boost it
                    confidence[key] = 1.0
                break  # first match wins per criterion

    data["selections"] = selections
    data["confidence"] = confidence
    return data


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

    # MEJORA 7: normalize involuccion_gerencia
    inv = selections.get("involuccion_gerencia")
    if inv and inv not in CRITERION_OPTIONS["involuccion_gerencia"]:
        selections["involuccion_gerencia"] = _apply_value_map(inv, INVOLUCCION_MAP)

    # MEJORA 7: normalize alcance_actividad
    alc = selections.get("alcance_actividad")
    if alc and alc not in CRITERION_OPTIONS["alcance_actividad"]:
        selections["alcance_actividad"] = _apply_value_map(alc, ALCANCE_MAP)

    data["selections"] = selections
    data["confidence"] = confidence
    return data


def _apply_logical_implications(data: dict) -> dict:
    """MEJORA 9: Infer additional criteria from already-confirmed selections.

    Deterministic logical rules derived from DPI semantics:
    - experiencia_internacional = "Ninguna"  →  num_paises = "Ninguno"
      (company has never exported → cannot be selling in any foreign countries)
    - alcance_actividad = "Internacional"  →  num_paises ≥ "De 1 a 5" when still null
      (selling internationally means at least 1 country)

    Called AFTER _boost_visual_confidence and BEFORE _null_low_confidence.
    """
    selections = data.get("selections", {})
    confidence = data.get("confidence", {})

    exp = selections.get("experiencia_internacional")
    if exp == "Ninguna" and not selections.get("num_paises"):
        selections["num_paises"] = "Ninguno"
        confidence["num_paises"] = 0.95

    alc = selections.get("alcance_actividad")
    if alc == "Internacional" and not selections.get("num_paises"):
        selections["num_paises"] = "De 1 a 5"
        confidence["num_paises"] = 0.75

    data["selections"] = selections
    data["confidence"] = confidence
    return data


def extract_dpi_fields(cuestionario_text: str, transcript_text: str = "") -> dict:
    """Extract DPI fields and selections from document text using Claude.

    Args:
        cuestionario_text: Raw text from the cuestionario PDF (up to 5000 chars).
        transcript_text: Optional transcript text from interview PDF (up to 3000 chars).

    Returns:
        dict with keys: direct_fields, selections, confidence
        Selections with confidence < 0.7 are set to null.
    """
    combined = f"=== CUESTIONARIO ===\n{cuestionario_text[:5000]}"
    if transcript_text:
        combined += f"\n\n=== TRANSCRIPCIÓN ENTREVISTA ===\n{transcript_text[:3000]}"
    prompt = EXTRACTOR_PROMPT.format(text=combined)
    raw = run_claude(prompt)
    data = _parse_json_response(raw)

    if not data:
        return {
            "direct_fields": {},
            "selections": {k: None for k in CRITERION_OPTIONS},
            "confidence": {k: 0.0 for k in CRITERION_OPTIONS},
        }

    data = _normalize_selections(data, combined)
    data = _boost_visual_confidence(data, cuestionario_text)  # MEJORA 8
    data = _apply_logical_implications(data)  # MEJORA 9
    return _null_low_confidence(data)
