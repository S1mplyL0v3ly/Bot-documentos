"""scoring_engine.py — DPI scoring from JSON matrix. No LLM required."""

from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

_MATRIX_PATH = Path(__file__).resolve().parent / "config" / "scoring_matrix.json"


def _load_matrix() -> dict:
    return json.loads(_MATRIX_PATH.read_text(encoding="utf-8"))


_MATRIX = _load_matrix()

# ── Public constants (consumed by orchestrator.py and extractor.py) ──────────

SCORE_MAP: dict[str, dict[str, int]] = {}
BLOCKS: dict[str, list[str]] = {}
BLOCKS_MAX: dict[str, int] = {}
CRITERION_OPTIONS: dict[str, list[str]] = {}

for _block in _MATRIX["blocks"]:
    _bname = _block["name"]
    BLOCKS[_bname] = []
    BLOCKS_MAX[_bname] = _block["max"]
    for _c in _block["criteria"]:
        _key = _c["key"]
        BLOCKS[_bname].append(_key)
        SCORE_MAP[_key] = {o["label"]: o["score"] for o in _c["options"]}
        CRITERION_OPTIONS[_key] = [o["label"] for o in _c["options"]]

for _c in _MATRIX.get("non_scoring_criteria", []):
    CRITERION_OPTIONS[_c["key"]] = _c["options"]

MAX_TOTAL: int = _MATRIX["max_total"]

# ── Fuzzy matching ────────────────────────────────────────────────────────────

_FUZZY_THRESHOLD = 0.82


def fuzzy_match_option(criterion: str, value: str) -> Optional[str]:
    """Return the canonical option label for *value*, or None if no confident match.

    Resolution order:
    1. Exact match (case-sensitive)
    2. Case-insensitive exact match
    3. Substring containment (value in option OR option in value)
    4. difflib ratio >= threshold
    """
    if criterion not in CRITERION_OPTIONS:
        return None
    options = CRITERION_OPTIONS[criterion]
    if not value:
        return None

    # 1. Exact
    if value in options:
        return value

    # 2. Case-insensitive exact
    value_lower = value.lower().strip()
    for opt in options:
        if opt.lower() == value_lower:
            return opt

    # 3. Substring
    for opt in options:
        opt_lower = opt.lower()
        if value_lower in opt_lower or opt_lower in value_lower:
            return opt

    # 4. Ratio
    best_ratio = 0.0
    best_opt = None
    for opt in options:
        ratio = SequenceMatcher(None, value_lower, opt.lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_opt = opt
    if best_ratio >= _FUZZY_THRESHOLD:
        return best_opt

    return None


def resolve_selections(raw: dict[str, Optional[str]]) -> dict[str, Optional[str]]:
    """Apply fuzzy matching to each selection value. Returns resolved dict."""
    resolved = {}
    for k, v in raw.items():
        if v is None:
            resolved[k] = None
            continue
        if k in CRITERION_OPTIONS:
            matched = fuzzy_match_option(k, v)
            resolved[k] = matched
        else:
            resolved[k] = v
    return resolved


# ── Scoring ───────────────────────────────────────────────────────────────────


def calculate_dpi_score(selections: dict[str, Optional[str]]) -> dict:
    """Calculate DPI score by block and total. Pure Python — no LLM.

    Returns:
        {
          "scores":    {"Económico": int, "Internacional": int, "Digitalización": int},
          "totals":    {"Económico": 15, "Internacional": 35, "Digitalización": 15},
          "total":     int,
          "max_total": 65,
          "pct":       int,
        }
    """
    scores: dict[str, int] = {}
    for block_name, criteria in BLOCKS.items():
        pts = 0
        for criterion in criteria:
            val = selections.get(criterion)
            if val:
                pts += SCORE_MAP.get(criterion, {}).get(val, 0)
        scores[block_name] = pts

    total = sum(scores.values())
    return {
        "scores": scores,
        "totals": dict(
            BLOCKS_MAX
        ),  # copy — never return mutable module-level dict by reference
        "total": total,
        "max_total": MAX_TOTAL,
        "pct": round(total / MAX_TOTAL * 100),
    }
