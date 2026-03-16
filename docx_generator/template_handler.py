"""DOCX template renderer using python-docx."""

from pathlib import Path

from docx import Document

from config import BASE_DIR, TEMPLATES_DIR

OUTPUT_DIR = BASE_DIR / "outputs"
DEFAULT_TEMPLATE = TEMPLATES_DIR / "plantilla.docx"


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


def render_template(
    document_id: int,
    fields: dict[str, str | None],
    document_type: str = "default",
) -> Path:
    """Fill template placeholders with extracted field values.

    Placeholders in the DOCX must use {{field_name}} syntax.

    Args:
        document_id: Used to name the output file uniquely.
        fields: Mapping of field_name → value (None replaced with empty string).
        document_type: Optional template variant name.

    Returns:
        Path to the generated DOCX file.
    """
    template_path = _find_template(document_type)
    doc = Document(str(template_path))

    for paragraph in doc.paragraphs:
        for field_name, value in fields.items():
            placeholder = f"{{{{{field_name}}}}}"
            if placeholder in paragraph.text:
                for run in paragraph.runs:
                    run.text = run.text.replace(placeholder, value or "")

    # Tablas también
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for field_name, value in fields.items():
                        placeholder = f"{{{{{field_name}}}}}"
                        if placeholder in paragraph.text:
                            for run in paragraph.runs:
                                run.text = run.text.replace(placeholder, value or "")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"documento_{document_id}.docx"
    doc.save(str(output_path))
    return output_path
