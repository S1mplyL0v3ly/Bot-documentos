"""Web scraper for DPI enrichment.

Flow:
  1. search_company_urls() — DuckDuckGo search, returns ranked URL candidates
  2. scrape_url() — httpx GET with 8s timeout, returns HTML (fails silently)
  3. _score_url_match() — 0.0–1.0 confidence that URL belongs to the company
  4. _build_dpi_from_web() — extract DPI signals from scraped content
  5. find_best_candidate() — orchestrate 1-4 and return best result dict
"""

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


def search_company_urls(company_name: str, max_results: int = 6) -> list[str]:
    """Return up to max_results candidate URLs via DuckDuckGo (no API key needed).

    Falls back to empty list on any error (no exceptions propagated).
    """
    try:
        from duckduckgo_search import DDGS  # type: ignore

        queries = [
            f'"{company_name}" site web oficial',
            f"{company_name} empresa Canarias",
        ]
        seen: list[str] = []
        with DDGS() as ddgs:
            for q in queries:
                for r in ddgs.text(q, max_results=max_results // len(queries) + 2):
                    href = r.get("href", "")
                    if href and href not in seen:
                        seen.append(href)
                    if len(seen) >= max_results:
                        return seen
        return seen
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 2. Scrape
# ---------------------------------------------------------------------------


def scrape_url(url: str) -> Optional[str]:
    """GET url with 8s timeout. Returns HTML string or None on failure."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; AutoreporteBot/1.0; +https://canariasexpande.es)"
        )
    }
    try:
        resp = httpx.get(
            url, headers=headers, timeout=_HTTP_TIMEOUT, follow_redirects=True
        )
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None


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
    """Search DuckDuckGo for '"{company_name}" CIF' and extract CIF from snippets.

    Returns the CIF string (e.g. 'B12345678') or None if not found.
    Never loads Einforma/Axesor pages — the CIF appears in result snippets.
    """
    try:
        from duckduckgo_search import DDGS  # type: ignore

        query = f'"{company_name}" CIF'
        with DDGS() as ddgs:
            for result in ddgs.text(query, max_results=8):
                # Check both snippet body and title
                for field in ("body", "title", "href"):
                    text = result.get(field, "") or ""
                    match = _CIF_PATTERN.search(text)
                    if match:
                        return match.group(1).upper()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 6. Main entry point
# ---------------------------------------------------------------------------


def find_best_candidate(
    company_name: str,
    cache: Optional[dict] = None,
) -> dict:
    """Search, scrape and score URLs for company_name.

    Args:
        company_name: Name from cuestionario Q1.
        cache: Previously stored search cache (skip search if set).

    Returns:
        {
            "url": str | None,         # best candidate URL
            "score": float,            # match confidence (0–1)
            "dpi_signals": dict,       # DPI selections/direct_fields extracted
            "search_cache": dict,      # raw search results for DB storage
        }
    """
    if cache:
        urls = cache.get("urls", [])
    else:
        urls = search_company_urls(company_name)

    search_cache = {"urls": urls}

    best_url: Optional[str] = None
    best_score: float = 0.0
    best_html: Optional[str] = None

    for url in urls:
        html = scrape_url(url)
        score = _score_url_match(url, html or "", company_name)
        if score > best_score:
            best_score = score
            best_url = url
            best_html = html

    dpi_signals: dict = {}
    if best_url and best_score >= _MIN_SCORE and best_html:
        dpi_signals = _build_dpi_from_web(best_url, best_html)

    # CIF fallback: if not found in scraped HTML, try DDG snippet search
    if not dpi_signals.get("direct_fields", {}).get("CIF"):
        cif = search_cif_ddg(company_name)
        if cif:
            dpi_signals.setdefault("direct_fields", {})["CIF"] = cif
            dpi_signals.setdefault("confidence", {})["CIF"] = 0.7

    return {
        "url": best_url if best_score >= _MIN_SCORE else None,
        "score": round(best_score, 3),
        "dpi_signals": dpi_signals,
        "search_cache": search_cache,
    }
