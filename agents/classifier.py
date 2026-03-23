"""Document type classifier using Claude headless."""

import json

from config import settings
from utils.dq_adapter import call_llm

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


def classify_document(text: str) -> dict:
    """Classify a document and return its type and expected fields.

    Args:
        text: Raw text extracted from the document.

    Returns:
        dict with keys: document_type, expected_fields, confidence
    """
    prompt = CLASSIFIER_PROMPT + text[:4000]  # limitar contexto
    raw = call_llm(prompt, tier=2)

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
