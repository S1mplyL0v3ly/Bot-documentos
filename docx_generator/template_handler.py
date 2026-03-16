"""DOCX template renderer for plantilla DPI Canarias.

INVARIANT: templates/plantilla.docx is NEVER modified.
render_template() always reads the original and writes a NEW file to outputs/.
"""

import re
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

# Claves DAFO que aparecen como placeholders {{}} en la plantilla
DAFO_KEYS = {
    "dafo_debilidades",
    "dafo_amenazas",
    "dafo_fortalezas",
    "dafo_oportunidades",
}


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


def _replace_in_paragraph(paragraph, replacements: dict[str, str]) -> None:
    """Replace {{key}} placeholders preserving run formatting."""
    for key, value in replacements.items():
        placeholder = f"{{{{{key}}}}}"
        if placeholder in paragraph.text:
            # Reconstruct runs to avoid split-placeholder issues
            full_text = paragraph.text
            new_text = full_text.replace(placeholder, value or "")
            if paragraph.runs:
                paragraph.runs[0].text = new_text
                for run in paragraph.runs[1:]:
                    run.text = ""


def _replace_marker_in_paragraph(paragraph, free_texts: dict[str, str]) -> None:
    """Replace [Poner texto] / [Redactar] markers with free text content."""
    for marker, key in FREE_TEXT_MARKERS.items():
        if marker in paragraph.text and key in free_texts:
            value = free_texts.get(key) or ""
            if paragraph.runs:
                paragraph.runs[0].text = paragraph.text.replace(marker, value)
                for run in paragraph.runs[1:]:
                    run.text = ""


def _apply_direct_fields_to_paragraphs(
    doc: Document, direct_fields: dict[str, str]
) -> None:
    """Replace {{placeholder}} in all paragraphs (body + table cells)."""
    all_paragraphs = list(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                all_paragraphs.extend(cell.paragraphs)

    for paragraph in all_paragraphs:
        _replace_in_paragraph(paragraph, direct_fields)


def _apply_free_texts_to_paragraphs(doc: Document, free_texts: dict[str, str]) -> None:
    """Replace markers and {{dafo_*}} placeholders with free text content."""
    # Merge DAFO keys into replacements for {{}} style placeholders
    dafo_replacements = {k: free_texts.get(k, "") for k in DAFO_KEYS}

    all_paragraphs = list(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                all_paragraphs.extend(cell.paragraphs)

    for paragraph in all_paragraphs:
        _replace_marker_in_paragraph(paragraph, free_texts)
        _replace_in_paragraph(paragraph, dafo_replacements)


def _apply_selections(doc: Document, selections: dict[str, str | None]) -> None:
    """Mark selected option cells with green background (#92D050).

    Iterates every cell in every table. If the cell text (stripped, lowercase)
    matches a selected value, applies green fill. Non-selected cells are untouched.
    """
    selected_values = {v.strip().lower() for v in selections.values() if v}
    if not selected_values:
        return

    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                cell_text = cell.text.strip().lower()
                if cell_text and cell_text in selected_values:
                    _set_cell_background(cell, GREEN_FILL)


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

    direct_fields: dict[str, str] = {
        k: (v or "") for k, v in data.get("direct_fields", {}).items()
    }
    selections: dict[str, str | None] = data.get("selections", {})
    free_texts: dict[str, str] = {
        k: (v or "") for k, v in data.get("free_texts", {}).items()
    }

    _apply_direct_fields_to_paragraphs(doc, direct_fields)
    _apply_selections(doc, selections)
    _apply_free_texts_to_paragraphs(doc, free_texts)

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
