"""Field extractor from documents using Claude headless."""

import json
import subprocess
from pathlib import Path
from typing import Optional

from config import settings

EXTRACTOR_PROMPT = """Extrae los siguientes campos del documento. Para cada campo devuelve el valor encontrado y un nivel de confianza del 0.0 al 1.0.

Campos a extraer: {fields}

Responde ÚNICAMENTE con JSON válido, sin markdown, con este formato exacto:
{{
  "extracted": {{
    "nombre_campo": {{"value": "valor encontrado o null", "confidence": 0.0}}
  }}
}}

Texto del documento:
{text}
"""


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

    Supports: .pdf, .docx, .txt, .png, .jpg, .jpeg (vision via Claude)
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

    # Imágenes → Claude vision (pasar path, Claude maneja multimodal)
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return f"[IMAGE_FILE:{file_path.as_posix()}]"

    return ""


def extract_fields(text: str, expected_fields: list[str]) -> dict[str, dict]:
    """Extract named fields from document text using Claude.

    Args:
        text: Raw text from the document.
        expected_fields: List of field names to extract.

    Returns:
        dict mapping field_name → {value, confidence}
    """
    fields_str = (
        ", ".join(expected_fields) if expected_fields else "todos los campos relevantes"
    )
    prompt = EXTRACTOR_PROMPT.format(
        fields=fields_str,
        text=text[:5000],
    )
    raw = run_claude(prompt)

    try:
        data = json.loads(raw)
        return data.get("extracted", {})
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            try:
                data = json.loads(raw[start:end])
                return data.get("extracted", {})
            except json.JSONDecodeError:
                pass
    return {}
