"""Pipeline unit tests for autoreporte DPI."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ─── Classifier (kept for compatibility) ─────────────────────────────────────


def test_classifier_identifies_document_type():
    """Classifier should return a document_type and expected_fields from text."""
    from agents.classifier import classify_document

    sample_text = (
        "FACTURA N.º 2024-001\n"
        "Emisor: Empresa S.L. CIF: B12345678\n"
        "Importe total: 1.210,00 EUR\n"
        "Fecha: 15/01/2024"
    )
    fake_response = json.dumps(
        {
            "document_type": "factura",
            "expected_fields": ["numero_factura", "emisor", "importe", "fecha"],
            "confidence": 0.95,
        }
    )
    with patch("agents.classifier.run_claude", return_value=fake_response):
        result = classify_document(sample_text)

    assert result["document_type"] == "factura"
    assert "numero_factura" in result["expected_fields"]
    assert result["confidence"] == 0.95


# ─── Extractor ────────────────────────────────────────────────────────────────


def test_extractor_returns_json_with_fields():
    """extract_dpi_fields should return direct_fields + selections + confidence."""
    from agents.extractor import extract_dpi_fields

    text = (
        "Empresa: Tech SL  CIF: B87654321  Web: www.tech.es\n"
        "Empleados: 15  Facturación: 800.000 €  Años actividad: 5\n"
        "Exporta a 3 países europeos."
    )
    fake_response = json.dumps(
        {
            "direct_fields": {
                "Razon_Social": "Tech SL",
                "CIF": "B87654321",
                "WEB": "www.tech.es",
                "Persona_Contacto": None,
                "Cargo": None,
                "email": None,
                "Telefono_Contacto": None,
            },
            "selections": {
                "situacion_empresa": "Más de 2 años",
                "num_empleados": "Más de 2",
                "facturacion": "Entre 500.000 y 1.000.000 €",
                "evolucion_facturacion": None,
                "recursos_internacionalizacion": None,
                "experiencia_internacional": "Menos de 3 años",
                "alcance_actividad": "Internacional",
                "num_paises": "De 1 a 5",
                "personal_dedicado": None,
                "involuccion_gerencia": None,
                "adaptacion_demanda": None,
                "adaptacion_producto": None,
                "tiene_web": "Sí",
                "ecommerce": None,
                "mercados_electronicos": None,
                "redes_sociales": None,
            },
            "confidence": {
                "situacion_empresa": 0.95,
                "num_empleados": 0.90,
                "facturacion": 0.80,
                "evolucion_facturacion": 0.4,
                "recursos_internacionalizacion": 0.3,
                "experiencia_internacional": 0.75,
                "alcance_actividad": 0.85,
                "num_paises": 0.88,
                "personal_dedicado": 0.3,
                "involuccion_gerencia": 0.2,
                "adaptacion_demanda": 0.4,
                "adaptacion_producto": 0.4,
                "tiene_web": 0.99,
                "ecommerce": 0.3,
                "mercados_electronicos": 0.2,
                "redes_sociales": 0.3,
            },
        }
    )

    with patch("agents.extractor.run_claude", return_value=fake_response):
        result = extract_dpi_fields(text)

    assert result["direct_fields"]["Razon_Social"] == "Tech SL"
    assert result["selections"]["situacion_empresa"] == "Más de 2 años"
    assert result["selections"]["tiene_web"] == "Sí"
    # Low confidence → nulled
    assert result["selections"]["evolucion_facturacion"] is None
    assert result["selections"]["personal_dedicado"] is None


def test_extractor_returns_null_for_low_confidence():
    """Selections with confidence < 0.7 must be nulled by _null_low_confidence."""
    from agents.extractor import _null_low_confidence

    data = {
        "direct_fields": {},
        "selections": {
            "situacion_empresa": "Más de 2 años",
            "num_empleados": "Más de 2",
            "facturacion": "Menos de 200.000 €",
        },
        "confidence": {
            "situacion_empresa": 0.9,  # keep
            "num_empleados": 0.65,  # null (below threshold)
            "facturacion": 0.5,  # null
        },
    }
    result = _null_low_confidence(data)
    assert result["selections"]["situacion_empresa"] == "Más de 2 años"
    assert result["selections"]["num_empleados"] is None
    assert result["selections"]["facturacion"] is None


# ─── DOCX Generator ───────────────────────────────────────────────────────────


def test_docx_generator_creates_file(tmp_path):
    """render_template should produce a .docx with direct fields and green selections."""
    from docx import Document
    from docx_generator.template_handler import render_template

    # Build minimal template with direct-field placeholder and a selection cell
    template = Document()
    template.add_paragraph("Empresa: {{Razon_Social}}")
    template.add_paragraph("CIF: {{CIF}}")
    # Table with selection options
    tbl = template.add_table(rows=2, cols=2)
    tbl.cell(0, 0).text = "Situación"
    tbl.cell(0, 1).text = "Más de 2 años"
    tbl.cell(1, 0).text = "Empleados"
    tbl.cell(1, 1).text = "Menos de 2"
    template_path = tmp_path / "plantilla.docx"
    template.save(str(template_path))

    data = {
        "direct_fields": {"Razon_Social": "Tech SL", "CIF": "B87654321"},
        "selections": {"situacion_empresa": "Más de 2 años"},
        "free_texts": {},
    }

    with (
        patch("docx_generator.template_handler.TEMPLATES_DIR", tmp_path),
        patch("docx_generator.template_handler.DEFAULT_TEMPLATE", template_path),
        patch("docx_generator.template_handler.OUTPUT_DIR", tmp_path),
    ):
        output = render_template(document_id=1, data=data, empresa_name="TechSL")

    assert output.exists()
    assert output.suffix == ".docx"
    doc = Document(str(output))
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert "Tech SL" in full_text
    assert "B87654321" in full_text


def test_render_template_marks_selection_green(tmp_path):
    """Selected option cell must receive green fill (#92D050), others untouched."""
    from docx import Document
    from docx.oxml.ns import qn
    from docx_generator.template_handler import GREEN_FILL, render_template

    template = Document()
    tbl = template.add_table(rows=3, cols=1)
    tbl.cell(0, 0).text = "No constituida"
    tbl.cell(1, 0).text = "Menos de 2 años"
    tbl.cell(2, 0).text = "Más de 2 años"
    template_path = tmp_path / "plantilla.docx"
    template.save(str(template_path))

    data = {
        "direct_fields": {},
        "selections": {"situacion_empresa": "Más de 2 años"},
        "free_texts": {},
    }

    with (
        patch("docx_generator.template_handler.TEMPLATES_DIR", tmp_path),
        patch("docx_generator.template_handler.DEFAULT_TEMPLATE", template_path),
        patch("docx_generator.template_handler.OUTPUT_DIR", tmp_path),
    ):
        output = render_template(document_id=2, data=data)

    doc = Document(str(output))
    cells = doc.tables[0].columns[0].cells

    def get_fill(cell) -> str:
        shd = cell._tc.find(f".//{{{qn('w:shd').split('}')[0][1:]}}}shd")
        if shd is None:
            # Try direct child
            tcPr = cell._tc.find(qn("w:tcPr"))
            if tcPr is None:
                return ""
            shd = tcPr.find(qn("w:shd"))
        return (shd.get(qn("w:fill")) or "") if shd is not None else ""

    selected_fill = get_fill(cells[2])  # "Más de 2 años"
    other_fill = get_fill(cells[0])  # "No constituida"

    assert selected_fill.upper() == GREEN_FILL.upper()
    assert other_fill.upper() != GREEN_FILL.upper()


# ─── Validator ────────────────────────────────────────────────────────────────


def test_validator_detects_missing_fields():
    """validate_fields should correctly identify missing required fields."""
    from docx_generator.validator import validate_fields

    fields = {
        "nombre": "Juan García",
        "fecha": "15/01/2024",
        "importe": None,
    }
    required = ["nombre", "fecha", "importe", "cif"]
    result = validate_fields(fields, required)

    assert result["complete"] is False
    assert "importe" in result["missing"]
    assert "cif" in result["missing"]
    assert "nombre" in result["present"]
    assert result["total_required"] == 4
    assert result["total_present"] == 2


# ─── Orchestrator ─────────────────────────────────────────────────────────────


def test_orchestrator_generates_questions_for_null_fields():
    """generate_questions should list only the criteria with null values."""
    from agents.orchestrator import PREFIX_SELECTION, generate_questions

    # Mock DB session
    mock_field_null = MagicMock()
    mock_field_null.field_name = f"{PREFIX_SELECTION}recursos_internacionalizacion"
    mock_field_null.field_value = None

    mock_field_ok = MagicMock()
    mock_field_ok.field_name = f"{PREFIX_SELECTION}situacion_empresa"
    mock_field_ok.field_value = "Más de 2 años"

    mock_db = MagicMock()
    with patch(
        "agents.orchestrator.crud.get_fields",
        return_value=[mock_field_null, mock_field_ok],
    ):
        message = generate_questions(mock_db, document_id=1)

    assert "recursos_internacionalizacion" in message or "recursos" in message.lower()
    assert "situacion_empresa" not in message


def test_approval_flow_generates_final_docx(tmp_path):
    """generate_final_docx should call render_template and update status to complete."""
    from agents.orchestrator import generate_final_docx

    fake_output = tmp_path / "informe_test.docx"
    fake_output.write_bytes(b"PK")  # minimal zip-like content

    mock_db = MagicMock()
    mock_fields = [
        MagicMock(field_name="dir_Razon_Social", field_value="Empresa Test"),
        MagicMock(field_name="sel_situacion_empresa", field_value="Más de 2 años"),
        MagicMock(field_name="txt_conclusiones", field_value="Buen potencial."),
    ]
    with (
        patch("agents.orchestrator.crud.get_fields", return_value=mock_fields),
        patch("agents.orchestrator.crud.create_or_update_generated_docx"),
        patch("agents.orchestrator.crud.update_document_status") as mock_status,
        patch(
            "agents.orchestrator.render_template", return_value=fake_output
        ) as mock_render,
        patch("agents.orchestrator._log_to_jarvis"),
    ):
        result = generate_final_docx(mock_db, document_id=1)

    assert result["status"] == "complete"
    assert "output_path" in result
    mock_render.assert_called_once()
    mock_status.assert_called_with(mock_db, 1, "complete")
