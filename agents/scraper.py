"""Scraper agent: fills missing fields via async HTTP search."""

import asyncio
from typing import Optional

import httpx

SEARCH_TIMEOUT = 15.0
HEADERS = {"User-Agent": "Mozilla/5.0 (autoreporte-bot/1.0)"}


async def _fetch_url(client: httpx.AsyncClient, url: str) -> str:
    """Fetch a URL and return text content."""
    try:
        response = await client.get(url, timeout=SEARCH_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        return response.text[:3000]
    except httpx.HTTPError:
        return ""


async def search_field_value(field_name: str, context: str) -> Optional[str]:
    """Try to find a missing field value from public web sources.

    Args:
        field_name: Name of the missing field (e.g. "CIF empresa").
        context: Additional context to narrow the search (e.g. company name).

    Returns:
        Found value string, or None if not found.
    """
    query = f"{field_name} {context}".strip()
    search_url = f"https://duckduckgo.com/html/?q={httpx.URL.__class__}&ia=web"

    # Búsqueda básica — extendible con APIs específicas por campo
    # Por ejemplo: BORME para CIF, Catastro para referencias catastrales, etc.
    async with httpx.AsyncClient(headers=HEADERS) as client:
        # Placeholder: en producción conectar a APIs específicas
        # (BORME, INE, Catastro, AEMET, etc.)
        _ = await _fetch_url(client, f"https://duckduckgo.com/html/?q={query}")

    # Sin fuente de datos configurada → devolver None para pedir al usuario
    return None


async def fill_missing_fields(
    missing_fields: list[str],
    context: dict[str, str],
) -> dict[str, Optional[str]]:
    """Attempt to fill multiple missing fields concurrently.

    Args:
        missing_fields: List of field names that couldn't be extracted.
        context: Already-extracted fields as context for search.

    Returns:
        dict mapping field_name → found_value (or None)
    """
    context_str = " ".join(f"{k}={v}" for k, v in context.items() if v)
    tasks = [search_field_value(field, context_str) for field in missing_fields]
    results = await asyncio.gather(*tasks)
    return dict(zip(missing_fields, results))
