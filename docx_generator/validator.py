"""Field completeness validator."""

from typing import Optional


def validate_fields(
    fields: dict[str, Optional[str]],
    required_fields: list[str],
) -> dict:
    """Check which required fields are missing or empty.

    Args:
        fields: Extracted field values (None or empty = missing).
        required_fields: List of field names that must be present.

    Returns:
        dict with keys: complete (bool), missing (list), present (list)
    """
    missing = [f for f in required_fields if not fields.get(f)]
    present = [f for f in required_fields if fields.get(f)]
    return {
        "complete": len(missing) == 0,
        "missing": missing,
        "present": present,
        "total_required": len(required_fields),
        "total_present": len(present),
    }
