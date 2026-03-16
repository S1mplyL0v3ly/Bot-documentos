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


def test_extractor_maps_facturacion_correctly():
    """MEJORA 1: Non-exact facturación range is normalized to the correct DPI option."""
    from agents.extractor import extract_dpi_fields

    text = "Facturación total durante el ejercicio 2024: Menos de 250.000 €"
    null_conf = {
        k: 0.0
        for k in [
            "situacion_empresa",
            "num_empleados",
            "facturacion",
            "evolucion_facturacion",
            "recursos_internacionalizacion",
            "experiencia_internacional",
            "alcance_actividad",
            "num_paises",
            "personal_dedicado",
            "involuccion_gerencia",
            "adaptacion_demanda",
            "adaptacion_producto",
            "tiene_web",
            "ecommerce",
            "mercados_electronicos",
            "redes_sociales",
        ]
    }
    null_sel = {k: None for k in null_conf}
    fake_response = json.dumps(
        {
            "direct_fields": {
                "Razon_Social": None,
                "CIF": None,
                "WEB": None,
                "Persona_Contacto": None,
                "Cargo": None,
                "email": None,
                "Telefono_Contacto": None,
                "sector": None,
                "producto_servicio": None,
                "año_inicio": None,
            },
            "selections": {**null_sel, "facturacion": "Menos de 250.000 €"},
            "confidence": {**null_conf, "facturacion": 0.9},
        }
    )
    with patch("agents.extractor.run_claude", return_value=fake_response):
        result = extract_dpi_fields(text)
    assert result["selections"]["facturacion"] == "Menos de 200.000 €"


def test_extractor_deduces_situacion_from_year():
    """MEJORA 3: situacion_empresa is inferred from año_inicio when Claude returns null."""
    from agents.extractor import extract_dpi_fields

    text = "Año de inicio de la actividad empresarial: 2020"
    null_conf = {
        k: 0.0
        for k in [
            "situacion_empresa",
            "num_empleados",
            "facturacion",
            "evolucion_facturacion",
            "recursos_internacionalizacion",
            "experiencia_internacional",
            "alcance_actividad",
            "num_paises",
            "personal_dedicado",
            "involuccion_gerencia",
            "adaptacion_demanda",
            "adaptacion_producto",
            "tiene_web",
            "ecommerce",
            "mercados_electronicos",
            "redes_sociales",
        ]
    }
    null_sel = {k: None for k in null_conf}
    fake_response = json.dumps(
        {
            "direct_fields": {
                "Razon_Social": None,
                "CIF": None,
                "WEB": None,
                "Persona_Contacto": None,
                "Cargo": None,
                "email": None,
                "Telefono_Contacto": None,
                "sector": None,
                "producto_servicio": None,
                "año_inicio": "2020",
            },
            "selections": null_sel,
            "confidence": null_conf,
        }
    )
    with patch("agents.extractor.run_claude", return_value=fake_response):
        result = extract_dpi_fields(text)
    assert result["selections"]["situacion_empresa"] == "Más de 2 años"
    assert result["confidence"]["situacion_empresa"] >= 0.9


def test_extractor_atelier_maria():
    """Cuestionario Canarias Expande: normalización de campos para Atelier Maria Secretos.

    Verifica que los normalizadores convierten respuestas de texto libre del cuestionario
    a los valores DPI exactos: facturación, experiencia, involucción gerencia, situación.
    """
    from agents.extractor import extract_dpi_fields

    fixture = Path(__file__).parent / "fixtures" / "atelier_text.txt"
    text = fixture.read_text(encoding="utf-8")

    # Claude devuelve valores que requieren normalización (texto libre del cuestionario)
    _conf_95 = {
        k: 0.95
        for k in [
            "situacion_empresa",
            "num_empleados",
            "facturacion",
            "evolucion_facturacion",
            "recursos_internacionalizacion",
            "experiencia_internacional",
            "alcance_actividad",
            "num_paises",
            "personal_dedicado",
            "involuccion_gerencia",
            "adaptacion_demanda",
            "adaptacion_producto",
            "tiene_web",
            "ecommerce",
            "mercados_electronicos",
            "redes_sociales",
        ]
    }
    fake_response = json.dumps(
        {
            "direct_fields": {
                "Razon_Social": "Atelier Maria Secretos SL",
                "CIF": None,
                "WEB": None,
                "Persona_Contacto": None,
                "Cargo": None,
                "email": None,
                "Telefono_Contacto": None,
                "sector": "Joyería y bisutería artesanal",
                "producto_servicio": "Joyería artesanal de plata",
                "año_inicio": "2020",
            },
            "selections": {
                "situacion_empresa": None,  # debe deducirse de año_inicio=2020
                "num_empleados": "Más de 2",
                "facturacion": "Menos de 250.000 €",  # debe normalizarse → "Menos de 200.000 €"
                "evolucion_facturacion": "En crecimiento",
                "recursos_internacionalizacion": "Sí",
                "experiencia_internacional": "No hemos exportado nunca",  # → "Ninguna"
                "alcance_actividad": "Nacional",
                "num_paises": "Ninguno",
                "personal_dedicado": "No",
                "involuccion_gerencia": "Directamente involucrada",  # → "Directamente involucrados"
                "adaptacion_demanda": "Media",
                "adaptacion_producto": "Alta",
                "tiene_web": "Sí",
                "ecommerce": "Sin tienda web propia",
                "mercados_electronicos": "Con presencia pero sin ventas",
                "redes_sociales": "Redes sociales activas y planificadas",
            },
            "confidence": _conf_95,
        }
    )

    with patch("agents.extractor.run_claude", return_value=fake_response):
        result = extract_dpi_fields(text)

    # Normalización de facturación
    assert result["selections"]["facturacion"] == "Menos de 200.000 €"
    # Normalización de experiencia
    assert result["selections"]["experiencia_internacional"] == "Ninguna"
    # Normalización de involucción gerencia
    assert result["selections"]["involuccion_gerencia"] == "Directamente involucrados"
    # Deducción situacion_empresa desde año_inicio=2020 (6 años → "Más de 2 años")
    assert result["selections"]["situacion_empresa"] == "Más de 2 años"
    assert result["confidence"]["situacion_empresa"] >= 0.9

    # Confidence alta para campos clave con dato explícito
    for campo in ["num_empleados", "facturacion", "evolucion_facturacion", "tiene_web"]:
        assert (
            result["confidence"][campo] >= 0.8
        ), f"{campo} confidence baja: {result['confidence'][campo]}"

    # Con este fixture todos los criterios deben tener valor — máx 1 nulo aceptable
    nulls = sum(1 for v in result["selections"].values() if v is None)
    assert (
        nulls <= 1
    ), f"Demasiados campos sin deducir: {nulls} — {[k for k,v in result['selections'].items() if v is None]}"


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


# ─── CAMBIO 8: Two new tests ──────────────────────────────────────────────────


def test_calculate_dpi_score_atelier_maria():
    """calculate_dpi_score debe devolver puntuaciones correctas para Atelier Maria."""
    from agents.orchestrator import calculate_dpi_score

    selections = {
        "situacion_empresa": "Más de 2 años",
        "num_empleados": "Más de 2",
        "facturacion": "Menos de 200.000 €",
        "evolucion_facturacion": "En crecimiento",
        "experiencia_internacional": "Ninguna",
        "alcance_actividad": "Nacional",
        "num_paises": "Ninguno",
        "involuccion_gerencia": "Directamente involucrados",
        "adaptacion_demanda": "Media",
        "adaptacion_producto": "Alta",
        "tiene_web": "Sí",
        "ecommerce": "Sin tienda web propia",
        "mercados_electronicos": "Con presencia pero sin ventas",
        "redes_sociales": "Redes sociales activas y planificadas",
    }
    score = calculate_dpi_score(selections)

    # Económico: 5 (Más de 2 años) + 5 (Más de 2) + 1 (Menos de 200.000 €) = 11
    assert score["scores"]["Económico"] == 11
    # Internacional: 0 (Ninguna) + 3 (Nacional) + 6 (En crecimiento) + 5 (Directamente) + 2 (Media) + 2 (Alta) + 0 (Ninguno) = 18
    assert score["scores"]["Internacional"] == 18
    # Digitalización: 3 (Sí) + 1 (Sin tienda) + 1 (Con presencia sin ventas) + 1 (Activas y planificadas) = 6
    assert score["scores"]["Digitalización"] == 6
    assert score["total"] == 35
    assert score["max_total"] == 65
    assert score["pct"] == 54  # round(35/65*100) = round(53.8) = 54


def test_two_document_flow():
    """get_document_waiting_transcript devuelve el doc en waiting_transcript del sender."""
    mock_doc = MagicMock()
    mock_doc.id = 42
    mock_doc.status = "waiting_transcript"
    mock_doc.transcript_text = None

    mock_db = MagicMock()

    with patch(
        "database.crud.get_document_by_sender_and_status", return_value=mock_doc
    ):
        from database.crud import get_document_waiting_transcript

        result = get_document_waiting_transcript(mock_db, sender_id="34600000000")

    assert result is mock_doc
    assert result.id == 42
    assert result.status == "waiting_transcript"
