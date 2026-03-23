"""Document type classifier using Claude headless."""

import json
import subprocess
from pathlib import Path

from config import settings

CLASSIFIER_PROMPT = """Analiza el siguiente texto extraído de un documento y determina:
1. Tipo de documento (factura, contrato, informe, formulario, certificado, otro)
2. Lista de campos que esperarías encontrar en ese tipo de documento

Responde ÚNICAMENTE con JSON válido, sin markdown, con este formato exacto:
{
  "document_type": "string",
  "expected_fields": ["campo1", "campo2", "campo3"],
  "confidence": 0.0
}

Texto del documento:
"""


def run_claude(prompt: str) -> str:
    """Execute Claude headless and return stdout."""
    result = subprocess.run(
        ["claude", "-p", prompt, "--model", settings.claude_model],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=settings.claude_timeout,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    return result.stdout.strip()


def classify_document(text: str) -> dict:
    """Classify a document and return its type and expected fields.

    Args:
        text: Raw text extracted from the document.

    Returns:
        dict with keys: document_type, expected_fields, confidence
    """
    prompt = CLASSIFIER_PROMPT + text[:4000]  # limitar contexto
    raw = run_claude(prompt)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Fallback si Claude devuelve texto extra
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            return json.loads(raw[start:end])
        return {
            "document_type": "desconocido",
            "expected_fields": [],
            "confidence": 0.0,
        }
