"""Tests for scoring_engine.py — deterministic DPI scoring."""

import json
from pathlib import Path

import pytest

# ─── Matrix completeness ──────────────────────────────────────────────────────


def test_scoring_matrix_loads():
    """scoring_matrix.json must exist and be valid JSON."""
    path = Path(__file__).resolve().parent.parent / "config" / "scoring_matrix.json"
    assert path.exists(), f"Not found: {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "blocks" in data
    assert "max_total" in data


def test_scoring_matrix_has_16_scoreable_criteria():
    """All 16 scoreable DPI criteria must be present in the matrix."""
    from scoring_engine import SCORE_MAP

    expected = {
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
    }
    assert expected == set(SCORE_MAP.keys()), (
        f"Missing: {expected - set(SCORE_MAP.keys())}\n"
        f"Extra:   {set(SCORE_MAP.keys()) - expected}"
    )


def test_criterion_options_has_16_criteria():
    """CRITERION_OPTIONS must include all 16 DPI criteria (14 scoreable + 2 non-scoring)."""
    from scoring_engine import CRITERION_OPTIONS

    assert len(CRITERION_OPTIONS) == 16


def test_max_score_is_35():
    """Sum of all block maxes must equal 35 (Eco 12 + Int 14 + Dig 9)."""
    from scoring_engine import BLOCKS_MAX

    assert sum(BLOCKS_MAX.values()) == 35


# ─── Deterministic scoring ────────────────────────────────────────────────────


def test_scoring_is_deterministic():
    """Same input always produces the same score — no randomness."""
    from scoring_engine import calculate_dpi_score

    selections = {
        "situacion_empresa": "Más de 2 años",
        "num_empleados": "Más de 2",
        "facturacion": "Entre 200.000 y 500.000 €",
        "experiencia_internacional": "Menos de 3 años",
        "alcance_actividad": "Nacional",
        "evolucion_facturacion": "En crecimiento",
        "involuccion_gerencia": "Medianamente involucrados",
        "adaptacion_demanda": "Alta",
        "adaptacion_producto": "Media",
        "num_paises": "De 1 a 5",
        "tiene_web": "Si",
        "ecommerce": "Tienda web propia con ventas bajas o irregulares",
        "mercados_electronicos": "Sin presencia en mercados electrónicos",
        "redes_sociales": "Redes sociales activas y planificadas",
    }
    result1 = calculate_dpi_score(selections)
    result2 = calculate_dpi_score(selections)
    assert result1 == result2


def test_all_max_scores_sum_correctly():
    """A selection of max options for every criterion must yield 35."""
    from scoring_engine import SCORE_MAP, calculate_dpi_score

    best = {k: max(v, key=v.get) for k, v in SCORE_MAP.items()}
    result = calculate_dpi_score(best)
    assert result["total"] == 35, f"Expected 35, got {result['total']}"


def test_all_zero_scores():
    """A selection of min options for every criterion must yield the minimum (4)."""
    from scoring_engine import SCORE_MAP, calculate_dpi_score

    worst = {k: min(v, key=v.get) for k, v in SCORE_MAP.items()}
    result = calculate_dpi_score(worst)
    assert (
        result["total"] == 4
    )  # num_empleados=1, facturacion=1, evolucion_facturacion=1, ecommerce=1


def test_unknown_value_scores_zero_not_raises():
    """An unrecognised value must score 0 and not raise an exception."""
    from scoring_engine import calculate_dpi_score

    result = calculate_dpi_score({"situacion_empresa": "valor_inventado"})
    assert result["scores"]["Económico"] == 0
    assert result["total"] == 0


# ─── Fuzzy matching ───────────────────────────────────────────────────────────


def test_fuzzy_match_exact():
    """Exact match returns the option unchanged."""
    from scoring_engine import fuzzy_match_option

    assert fuzzy_match_option("situacion_empresa", "Más de 2 años") == "Más de 2 años"


def test_fuzzy_match_substring():
    """Partial string that uniquely identifies an option is resolved."""
    from scoring_engine import fuzzy_match_option

    # "Más de 5" is a substring of "Más de 5 años"
    result = fuzzy_match_option("experiencia_internacional", "Más de 5")
    assert result == "Más de 5 años"


def test_fuzzy_match_case_insensitive():
    """Case differences are normalised."""
    from scoring_engine import fuzzy_match_option

    result = fuzzy_match_option("alcance_actividad", "nacional")
    assert result == "Nacional"


def test_fuzzy_match_no_match_returns_none():
    """Completely unrelated value returns None — never guesses wrong."""
    from scoring_engine import fuzzy_match_option

    result = fuzzy_match_option("situacion_empresa", "xyz_completely_unrelated")
    assert result is None


def test_fuzzy_match_unknown_criterion_returns_none():
    """Unknown criterion key returns None without raising."""
    from scoring_engine import fuzzy_match_option

    result = fuzzy_match_option("not_a_real_criterion", "some value")
    assert result is None
