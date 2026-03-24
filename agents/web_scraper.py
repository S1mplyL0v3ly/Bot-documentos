"""Web scraper for DPI enrichment.

Flow:
  1. search_company_urls() — DuckDuckGo search, returns ranked URL candidates
  2. _scrape_one_async() — httpx async GET, returns (url, html|None)
  3. _score_url_match() — 0.0–1.0 confidence that URL belongs to the company
  4. _build_dpi_from_web() — extract DPI signals from scraped content
  5. find_best_candidate() — orchestrate 1-4 async/parallel, return best result dict
"""

import asyncio
import json
import re
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HTTP_TIMEOUT = 8  # seconds
_MIN_SCORE = 0.35  # minimum URL match score to propose to consultant

_CIF_PATTERN = re.compile(r"\b([ABCDEFGHJKLMNPQRSUVW]\d{7}[0-9A-J])\b", re.IGNORECASE)

_SOCIAL_PATTERNS = {
    "instagram": re.compile(r"instagram\.com/[^/\"'\s]+", re.IGNORECASE),
    "facebook": re.compile(r"facebook\.com/[^/\"'\s]+", re.IGNORECASE),
    "linkedin": re.compile(r"linkedin\.com/company/[^/\"'\s]+", re.IGNORECASE),
    "twitter": re.compile(r"(?:twitter|x)\.com/[^/\"'\s]+", re.IGNORECASE),
    "tiktok": re.compile(r"tiktok\.com/@[^/\"'\s]+", re.IGNORECASE),
}

_MARKETPLACE_PATTERNS = {
    "amazon": re.compile(r"amazon\.", re.IGNORECASE),
    "etsy": re.compile(r"etsy\.com", re.IGNORECASE),
    "ebay": re.compile(r"ebay\.", re.IGNORECASE),
    "alibaba": re.compile(r"alibaba\.com", re.IGNORECASE),
    "aliexpress": re.compile(r"aliexpress\.com", re.IGNORECASE),
}

_ECOMMERCE_SIGNALS = re.compile(
    r"(?:add.?to.?cart|add.?to.?basket|comprar|carrito|checkout|shop|tienda|"
    r"woocommerce|shopify|prestashop|magento|buy.?now|precio|price)",
    re.IGNORECASE,
)

_LANG_SELECTOR_SIGNALS = re.compile(
    r"(?:lang=|hreflang=|language.?selector|select.?country|"
    r"english|français|deutsch|italiano|português)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Lowercase, strip accents, keep only alphanumeric + spaces."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", " ", no_accents).strip()


def _tokenize(text: str) -> set[str]:
    return {w for w in _normalize(text).split() if len(w) > 2}


# ---------------------------------------------------------------------------
# 1. Search
# ---------------------------------------------------------------------------


def _ddgs_search_one(query: str, max_results: int) -> list[str]:
    """Run one DDGS text search synchronously — called via asyncio.to_thread."""
    try:
        from ddgs import DDGS  # type: ignore

        with DDGS() as ddgs:
            return [
                r.get("href", "")
                for r in ddgs.text(query, max_results=max_results)
                if r.get("href")
            ]
    except Exception:
        return []


async def _search_company_urls_async(
    company_name: str, max_results: int = 6
) -> list[str]:
    """Run 2 DDGS queries in parallel, deduplicate, return up to max_results URLs."""
    queries = [
        f'"{company_name}" site web oficial',
        f"{company_name} empresa Canarias",
    ]
    per_query = max_results // len(queries) + 2
    results = await asyncio.gather(
        asyncio.to_thread(_ddgs_search_one, queries[0], per_query),
        asyncio.to_thread(_ddgs_search_one, queries[1], per_query),
    )
    seen: list[str] = []
    for batch in results:
        for url in batch:
            if url and url not in seen:
                seen.append(url)
            if len(seen) >= max_results:
                return seen
    return seen


def search_company_urls(company_name: str, max_results: int = 6) -> list[str]:
    """Return up to max_results candidate URLs (runs 2 DuckDuckGo queries in parallel)."""
    try:
        return asyncio.run(_search_company_urls_async(company_name, max_results))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 2. Scrape
# ---------------------------------------------------------------------------


async def _scrape_one_async(
    client: httpx.AsyncClient, url: str
) -> tuple[str, str | None]:
    """Scrape a single URL asynchronously, return (url, html|None)."""
    try:
        resp = await client.get(url, timeout=_HTTP_TIMEOUT, follow_redirects=True)
        if resp.status_code == 200:
            return url, resp.text
    except Exception:
        pass
    return url, None


# ---------------------------------------------------------------------------
# 3. Score URL match
# ---------------------------------------------------------------------------


def _score_url_match(url: str, html: str, company_name: str) -> float:
    """Return 0.0–1.0 confidence that url is the official site of company_name."""
    score = 0.0
    tokens = _tokenize(company_name)
    parsed = urlparse(url)
    domain = _normalize(parsed.netloc)

    # Domain contains company tokens (highest signal)
    matched_in_domain = tokens & _tokenize(domain)
    score += min(len(matched_in_domain) / max(len(tokens), 1), 1.0) * 0.5

    if not html:
        return score

    soup = BeautifulSoup(html, "lxml")
    page_text = _normalize(soup.get_text(" ", strip=True))
    page_tokens = _tokenize(page_text)

    # Company name tokens appear in page body
    matched_in_body = tokens & page_tokens
    score += min(len(matched_in_body) / max(len(tokens), 1), 1.0) * 0.3

    # Title tag
    title_tag = soup.find("title")
    if title_tag:
        title_text = _normalize(title_tag.get_text())
        if tokens & _tokenize(title_text):
            score += 0.2

    return min(score, 1.0)


# ---------------------------------------------------------------------------
# 4. Build DPI signals from web
# ---------------------------------------------------------------------------


def _build_dpi_from_web(url: str, html: str) -> dict:
    """Extract DPI-relevant signals from scraped HTML.

    Returns a dict compatible with the selections/direct_fields schema.
    """
    signals: dict = {"direct_fields": {}, "selections": {}, "confidence": {}}
    if not html:
        return signals

    soup = BeautifulSoup(html, "lxml")
    full_text = soup.get_text(" ", strip=True)

    # --- WEB confirmed ---
    signals["selections"]["tiene_web"] = "Si"
    signals["confidence"]["tiene_web"] = 1.0
    signals["direct_fields"]["WEB"] = url

    # --- CIF (footer / legal notice) ---
    cif_match = _CIF_PATTERN.search(full_text)
    if cif_match:
        signals["direct_fields"]["CIF"] = cif_match.group(1).upper()

    # --- Redes sociales ---
    found_socials = []
    for name, pattern in _SOCIAL_PATTERNS.items():
        if pattern.search(html):
            found_socials.append(name)

    if len(found_socials) >= 2:
        signals["selections"][
            "redes_sociales"
        ] = "Redes sociales activas y planificadas"
        signals["confidence"]["redes_sociales"] = 0.75
    elif len(found_socials) == 1:
        signals["selections"][
            "redes_sociales"
        ] = "Redes sociales activas y planificadas"
        signals["confidence"]["redes_sociales"] = 0.6

    # --- Ecommerce ---
    has_cart = bool(_ECOMMERCE_SIGNALS.search(html))
    if has_cart:
        signals["selections"][
            "ecommerce"
        ] = "Tienda web propia con ventas bajas o irregulares"
        signals["confidence"]["ecommerce"] = 0.7

    # --- Marketplaces ---
    found_marketplaces = [n for n, p in _MARKETPLACE_PATTERNS.items() if p.search(html)]
    if found_marketplaces:
        signals["selections"][
            "mercados_electronicos"
        ] = "Con presencia en mercados electrónicos sin ventas o ventas bajas."
        signals["confidence"]["mercados_electronicos"] = 0.65

    # --- Alcance internacional (language selector = exports) ---
    if _LANG_SELECTOR_SIGNALS.search(html):
        signals["selections"]["alcance_actividad"] = "Internacional"
        signals["confidence"]["alcance_actividad"] = 0.7

    return signals


# ---------------------------------------------------------------------------
# 5. CIF search via DuckDuckGo snippets (no anti-bot page loads)
# ---------------------------------------------------------------------------


def search_cif_ddg(company_name: str) -> Optional[str]:
    """Search DuckDuckGo with 3 queries and extract CIF from snippets.

    Returns the CIF string (e.g. 'B12345678') or None if not found.
    Tries snippets only — no page loads to avoid anti-bot blocks.
    """
    queries = [
        f'"{company_name}" CIF',
        f'"{company_name}" NIF empresa',
        f'"{company_name}" registro mercantil',
    ]
    try:
        from ddgs import DDGS  # type: ignore

        with DDGS() as ddgs:
            for query in queries:
                for result in ddgs.text(query, max_results=5):
                    for field in ("body", "title", "href"):
                        text = result.get(field, "") or ""
                        match = _CIF_PATTERN.search(text)
                        if match:
                            return match.group(1).upper()
    except Exception:
        pass
    return None


def search_cif_infocif(company_name: str) -> Optional[str]:
    """Search infocif.es for CIF by company name (specialised registry source).

    Returns the CIF string or None if not found / blocked.
    """
    try:
        query = company_name.replace(" ", "+")
        url = f"https://www.infocif.es/buscar-empresas?q={query}"
        resp = httpx.get(
            url,
            timeout=10,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        match = _CIF_PATTERN.search(resp.text)
        if match:
            return match.group(1).upper()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 6. Sector context enrichment for conclusions
# ---------------------------------------------------------------------------


def enrich_conclusions_context(sector: str, company_name: str = "") -> str:
    """Quick DDG search for sector internationalisation context (~5s, $0 cost).

    Returns up to 1000 chars of real-world context to inject into the
    conclusion prompt. Returns empty string on any failure.
    """
    if not sector or sector == "No especificado":
        return ""
    try:
        from ddgs import DDGS  # type: ignore

        query = f"{sector} internacionalización pymes España 2025"
        urls: list[str] = []
        with DDGS() as ddgs:
            for result in ddgs.text(query, max_results=3):
                href = result.get("href", "")
                if href:
                    urls.append(href)

        context_parts: list[str] = []
        headers = {"User-Agent": "Mozilla/5.0"}
        for url in urls[:2]:
            try:
                resp = httpx.get(url, timeout=8, follow_redirects=True, headers=headers)
                soup = BeautifulSoup(resp.text, "html.parser")
                text = soup.get_text(separator=" ", strip=True)
                # Keep the first 500 chars that contain useful content
                snippet = text[:600].strip()
                if snippet:
                    context_parts.append(snippet)
            except Exception:
                continue

        combined = "\n".join(context_parts)
        return combined[:1000]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 7. Main entry point
# ---------------------------------------------------------------------------


async def find_best_candidate(
    company_name: str,
    cache: dict | None = None,
) -> dict:
    """Search, scrape and score URLs for company_name (async, parallel).

    Async because it is called from trigger_web_search() which is async.
    Using asyncio.run() inside an async context raises RuntimeError.

    Returns:
        {
            "url": str | None,
            "score": float,
            "dpi_signals": dict,
            "search_cache": dict,
        }
    """
    if cache:
        urls = cache.get("urls", [])
    else:
        urls = await _search_company_urls_async(company_name)

    search_cache = {"urls": urls}

    best_url: str | None = None
    best_score: float = 0.0
    best_html: str | None = None

    if urls:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AutoreporteBot/1.0)"}
        async with httpx.AsyncClient(headers=headers) as client:
            scrape_results = await asyncio.gather(
                *[_scrape_one_async(client, url) for url in urls]
            )
        for url, html in scrape_results:
            score = _score_url_match(url, html or "", company_name)
            if score > best_score:
                best_score = score
                best_url = url
                best_html = html

    dpi_signals: dict = {}
    if best_url and best_score >= _MIN_SCORE and best_html:
        dpi_signals = _build_dpi_from_web(best_url, best_html)

    # CIF fallback: DDG snippets first, then infocif.es
    if not dpi_signals.get("direct_fields", {}).get("CIF"):
        cif = await asyncio.to_thread(search_cif_ddg, company_name)
        if not cif:
            cif = await asyncio.to_thread(search_cif_infocif, company_name)
        if cif:
            dpi_signals.setdefault("direct_fields", {})["CIF"] = cif
            dpi_signals.setdefault("confidence", {})["CIF"] = 0.7

    return {
        "url": best_url if best_score >= _MIN_SCORE else None,
        "score": round(best_score, 3),
        "dpi_signals": dpi_signals,
        "search_cache": search_cache,
    }
