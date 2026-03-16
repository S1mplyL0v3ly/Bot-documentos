"""DOCX template renderer for plantilla DPI Canarias.

INVARIANT: templates/plantilla.docx is NEVER modified.
render_template() always reads the original and writes a NEW file to outputs/.
"""

import re
import unicodedata
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from config import BASE_DIR, TEMPLATES_DIR

OUTPUT_DIR = BASE_DIR / "outputs"
DEFAULT_TEMPLATE = TEMPLATES_DIR / "plantilla.docx"
GREEN_FILL = "92D050"  # color celda seleccionada

# Markers en párrafos libres → clave en free_texts
FREE_TEXT_MARKERS = {
    "[Poner texto]": "definicion_potencial",
    "[Redactar]": "conclusiones",
}

# Claves DAFO que aparecen como placeholders {{}} en la plantilla (fallback)
DAFO_KEYS = {
    "dafo_debilidades",
    "dafo_amenazas",
    "dafo_fortalezas",
    "dafo_oportunidades",
}

# BUG 3 fix: positional mapping (row_idx, col_idx) → free_text key
_DAFO_POSITIONS: dict[tuple[int, int], str] = {
    (2, 0): "dafo_debilidades",
    (2, 1): "dafo_amenazas",
    (3, 0): "dafo_fortalezas",
    (3, 1): "dafo_oportunidades",
}

_DAFO_HEADER_KEYWORDS = {
    "dafo",
    "debilidades",
    "fortalezas",
    "amenazas",
    "oportunidades",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _normalize_text(text: str) -> str:
    """Strip, lowercase, and remove combining accent marks for robust Spanish comparison."""
    nfkd = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _find_template(document_type: str = "default") -> Path:
    """Locate the right template file for the document type."""
    specific = TEMPLATES_DIR / f"{document_type}.docx"
    if specific.exists():
        return specific
    if DEFAULT_TEMPLATE.exists():
        return DEFAULT_TEMPLATE
    raise FileNotFoundError(
        f"No template found in {TEMPLATES_DIR}. "
        "Upload a 'plantilla.docx' to /root/autoreporte/templates/"
    )


def _set_cell_background(cell, color_hex: str) -> None:
    """Apply XML shading to a table cell (background fill)."""
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    for existing in tcPr.findall(qn("w:shd")):
        tcPr.remove(existing)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), color_hex.lstrip("#"))
    tcPr.append(shd)


def _set_cell_text(cell, text: str) -> None:
    """Write text into a cell's first paragraph, collapsing runs."""
    paragraphs = cell.paragraphs
    if not paragraphs:
        return
    p = paragraphs[0]
    if p.runs:
        p.runs[0].text = text
        for run in p.runs[1:]:
            run.text = ""
    else:
        p.add_run(text)
    for para in paragraphs[1:]:
        for run in para.runs:
            run.text = ""


def _replace_in_paragraph(paragraph, replacements: dict[str, str]) -> None:
    """Replace {{key}} placeholders with case-insensitive key lookup."""
    full_text = paragraph.text
    if "{{" not in full_text:
        return

    # BUG 1 fix: case-insensitive key matching
    lower_map = {k.lower(): v for k, v in replacements.items()}
    new_text = full_text
    for m in re.finditer(r"\{\{([^}]+)\}\}", full_text):
        value = lower_map.get(m.group(1).lower())
        if value is not None:
            new_text = new_text.replace(m.group(0), value)

    if new_text != full_text and paragraph.runs:
        paragraph.runs[0].text = new_text
        for run in paragraph.runs[1:]:
            run.text = ""


def _replace_marker_in_paragraph(paragraph, free_texts: dict[str, str]) -> None:
    """Replace [Poner texto] / [Redactar] markers with free text content."""
    full_text = paragraph.text
    new_text = full_text
    for marker, key in FREE_TEXT_MARKERS.items():
        if marker in new_text and key in free_texts:
            new_text = new_text.replace(marker, free_texts[key] or "")
    if new_text != full_text and paragraph.runs:
        paragraph.runs[0].text = new_text
        for run in paragraph.runs[1:]:
            run.text = ""


def _iter_all_paragraphs(doc: Document):
    """Yield every paragraph in the document: body + all table cells."""
    yield from doc.paragraphs
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs


def _apply_direct_fields_to_paragraphs(
    doc: Document, direct_fields: dict[str, str]
) -> None:
    """Replace {{placeholder}} in all paragraphs (body + table cells)."""
    for paragraph in _iter_all_paragraphs(doc):
        _replace_in_paragraph(paragraph, direct_fields)


def _apply_free_texts_to_paragraphs(doc: Document, free_texts: dict[str, str]) -> None:
    """Replace [Poner texto]/[Redactar] markers and {{dafo_*}} fallback placeholders."""
    dafo_replacements = {k: free_texts.get(k, "") for k in DAFO_KEYS}
    for paragraph in _iter_all_paragraphs(doc):
        _replace_marker_in_paragraph(paragraph, free_texts)  # BUG 4 fix
        _replace_in_paragraph(paragraph, dafo_replacements)  # {{dafo_*}} fallback


def _find_dafo_table(doc: Document):
    """Find the DAFO table by looking for keyword headers in its first 3 rows."""
    for table in doc.tables:
        for row in table.rows[:3]:
            for cell in row.cells:
                if any(kw in cell.text.strip().lower() for kw in _DAFO_HEADER_KEYWORDS):
                    return table
    return None


def _apply_dafo_direct(doc: Document, free_texts: dict[str, str]) -> None:
    """BUG 3 fix: Fill DAFO table cells by (row, col) position mapping."""
    dafo_table = _find_dafo_table(doc)
    if dafo_table is None:
        return
    rows = dafo_table.rows
    for (row_idx, col_idx), key in _DAFO_POSITIONS.items():
        value = free_texts.get(key, "")
        if row_idx < len(rows) and col_idx < len(rows[row_idx].cells):
            _set_cell_text(rows[row_idx].cells[col_idx], value)


def _apply_selections(doc: Document, selections: dict[str, str | None]) -> None:
    """BUG 2 fix: Mark selected option cells green using accent-normalized comparison.

    - Uses unicodedata normalization so 'Si' matches 'Sí'.
    - Skips {{valorar}} / [Valorar] placeholder cells.
    - Deduplicates merged cells via cell identity to avoid double-coloring.
    """
    normalized_selected: set[str] = {
        _normalize_text(v) for v in selections.values() if v
    }
    if not normalized_selected:
        return

    for table in doc.tables:
        for row in table.rows:
            seen: set[int] = set()
            for cell in row.cells:
                cell_id = id(cell._tc)
                if cell_id in seen:
                    continue
                seen.add(cell_id)

                raw = cell.text.strip()
                if not raw:
                    continue
                # Skip placeholder markers — not real option text
                if _normalize_text(raw) in ("{{valorar}}", "[valorar]", "valorar"):
                    continue
                if _normalize_text(raw) in normalized_selected:
                    _set_cell_background(cell, GREEN_FILL)


# ─── Public API ───────────────────────────────────────────────────────────────


def render_template(
    document_id: int,
    data: dict,
    empresa_name: str = "",
    document_type: str = "default",
) -> Path:
    """Fill the DPI template with direct fields, selections, and free texts.

    Args:
        document_id: Used to name the output file uniquely.
        data: dict with keys:
            - direct_fields: {Razon_Social, CIF, WEB, ...}
            - selections: {situacion_empresa: "opción elegida", ...}
            - free_texts: {definicion_potencial, conclusiones, dafo_*}
        empresa_name: Used in output filename (sanitized).
        document_type: Template variant name (default = plantilla.docx).

    Returns:
        Path to the generated DOCX file.
    """
    template_path = _find_template(document_type)
    doc = Document(str(template_path))

    # BUG 1 fix: pass direct_fields as-is; _replace_in_paragraph handles case-insensitivity.
    # Add 'fecha' fallback (today's date) when the field is absent from the document.
    raw_direct = data.get("direct_fields", {})
    direct_fields: dict[str, str] = {k: (v or "") for k, v in raw_direct.items()}
    if not any(k.lower() == "fecha" for k in raw_direct):
        direct_fields["fecha"] = datetime.now().strftime("%d/%m/%Y")

    selections: dict[str, str | None] = data.get("selections", {})
    free_texts: dict[str, str] = {
        k: (v or "") for k, v in data.get("free_texts", {}).items()
    }

    _apply_direct_fields_to_paragraphs(doc, direct_fields)
    _apply_selections(doc, selections)
    _apply_dafo_direct(doc, free_texts)  # positional fill (BUG 3)
    _apply_free_texts_to_paragraphs(
        doc, free_texts
    )  # markers + {{dafo_*}} fallback (BUG 4)

    # Build output filename — template original is NEVER touched
    empresa_clean = re.sub(r"[^\w\s-]", "", empresa_name).strip()
    empresa_clean = re.sub(r"\s+", " ", empresa_clean)
    filename = (
        f"Reporte {empresa_clean}.docx"
        if empresa_clean
        else f"Reporte {document_id}.docx"
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / filename
    doc.save(str(output_path))
    return output_path
