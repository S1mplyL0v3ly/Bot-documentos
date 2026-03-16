"""Pipeline unit tests for autoreporte."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# --- Classifier ---


def test_classifier_identifies_document_type():
    """Classifier should return a document_type and expected_fields from text."""
    from agents.classifier import classify_document

    sample_text = (
        "FACTURA N.º 2024-001\n"
        "Emisor: Empresa S.L. CIF: B12345678\n"
        "Receptor: Cliente SAU NIF: A87654321\n"
        "Importe total: 1.210,00 EUR\n"
        "Fecha: 15/01/2024"
    )

    fake_response = json.dumps(
        {
            "document_type": "factura",
            "expected_fields": [
                "numero_factura",
                "emisor",
                "receptor",
                "importe",
                "fecha",
            ],
            "confidence": 0.95,
        }
    )

    with patch("agents.classifier.run_claude", return_value=fake_response):
        result = classify_document(sample_text)

    assert result["document_type"] == "factura"
    assert "numero_factura" in result["expected_fields"]
    assert result["confidence"] == 0.95


# --- Extractor ---


def test_extractor_returns_json_with_fields():
    """Extractor should return a dict with field values and confidences."""
    from agents.extractor import extract_fields

    text = "Número factura: 2024-001\nFecha: 15/01/2024\nTotal: 1210.00 EUR"
    fields = ["numero_factura", "fecha", "importe_total"]

    fake_response = json.dumps(
        {
            "extracted": {
                "numero_factura": {"value": "2024-001", "confidence": 0.99},
                "fecha": {"value": "15/01/2024", "confidence": 0.95},
                "importe_total": {"value": "1210.00", "confidence": 0.90},
            }
        }
    )

    with patch("agents.extractor.run_claude", return_value=fake_response):
        result = extract_fields(text, fields)

    assert result["numero_factura"]["value"] == "2024-001"
    assert result["fecha"]["confidence"] == 0.95
    assert "importe_total" in result


# --- DOCX Generator ---


def test_docx_generator_creates_file(tmp_path):
    """render_template should produce a .docx file at the output path."""
    from docx import Document
    from docx_generator.template_handler import render_template

    # Crear plantilla mínima en tmp_path
    template = Document()
    template.add_paragraph("Nombre: {{nombre}}")
    template.add_paragraph("Fecha: {{fecha}}")
    template_path = tmp_path / "plantilla.docx"
    template.save(str(template_path))

    fields = {"nombre": "Juan García", "fecha": "15/01/2024"}

    with (
        patch("docx_generator.template_handler.TEMPLATES_DIR", tmp_path),
        patch("docx_generator.template_handler.DEFAULT_TEMPLATE", template_path),
        patch("docx_generator.template_handler.OUTPUT_DIR", tmp_path),
    ):
        output = render_template(document_id=1, fields=fields)

    assert output.exists()
    assert output.suffix == ".docx"

    # Verificar que los placeholders fueron reemplazados
    doc = Document(str(output))
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert "Juan García" in full_text
    assert "15/01/2024" in full_text


# --- Validator ---


def test_validator_detects_missing_fields():
    """validate_fields should correctly identify missing required fields."""
    from docx_generator.validator import validate_fields

    fields = {
        "nombre": "Juan García",
        "fecha": "15/01/2024",
        "importe": None,  # faltante
    }
    required = ["nombre", "fecha", "importe", "cif"]

    result = validate_fields(fields, required)

    assert result["complete"] is False
    assert "importe" in result["missing"]
    assert "cif" in result["missing"]
    assert "nombre" in result["present"]
    assert result["total_required"] == 4
    assert result["total_present"] == 2
