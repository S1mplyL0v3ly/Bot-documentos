"""Field value formatters for DOCX generation."""

import re
from datetime import datetime
from typing import Optional


def format_date(value: Optional[str]) -> str:
    """Normalize date strings to DD/MM/YYYY format."""
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value.strip(), fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return value


def format_amount(value: Optional[str]) -> str:
    """Format numeric amounts with dot thousands separator and comma decimal."""
    if not value:
        return ""
    cleaned = re.sub(r"[^\d.,]", "", value)
    try:
        number = float(cleaned.replace(",", "."))
        return f"{number:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except ValueError:
        return value


def format_nif(value: Optional[str]) -> str:
    """Uppercase and strip NIF/CIF/NIE identifiers."""
    if not value:
        return ""
    return re.sub(r"\s+", "", value).upper()


def apply_formatters(
    fields: dict[str, Optional[str]], field_types: dict[str, str]
) -> dict[str, str]:
    """Apply per-field formatters based on field_types mapping.

    Args:
        fields: Raw field values.
        field_types: Mapping of field_name → type (date|amount|nif|text).

    Returns:
        New dict with formatted values.
    """
    result = {}
    for name, value in fields.items():
        ftype = field_types.get(name, "text")
        if ftype == "date":
            result[name] = format_date(value)
        elif ftype == "amount":
            result[name] = format_amount(value)
        elif ftype == "nif":
            result[name] = format_nif(value)
        else:
            result[name] = value or ""
    return result
