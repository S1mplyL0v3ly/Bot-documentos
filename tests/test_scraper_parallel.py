"""Tests for web_scraper.py — parallel search and scraping."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def test_find_best_candidate_returns_expected_shape():
    """find_best_candidate() must return the dict shape the orchestrator expects."""
    from agents.web_scraper import find_best_candidate

    # find_best_candidate is async — use asyncio.run(). cache= skips real network calls.
    result = asyncio.run(find_best_candidate("Test Corp", cache={"urls": []}))
    assert "url" in result
    assert "score" in result
    assert "dpi_signals" in result
    assert "search_cache" in result


def test_find_best_candidate_with_mock_urls():
    """When URLs are provided via cache, scraping runs and returns best score."""
    from agents.web_scraper import find_best_candidate

    with patch("agents.web_scraper.httpx.AsyncClient") as mock_client_cls:
        # Mock async context manager
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_client_cls.return_value.__aexit__.return_value = None

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = (
            "<html><title>Test Corp</title><body>Test Corp official</body></html>"
        )
        mock_client.get = AsyncMock(return_value=mock_resp)

        # find_best_candidate is async
        result = asyncio.run(
            find_best_candidate("Test Corp", cache={"urls": ["https://testcorp.com"]})
        )

    assert isinstance(result["score"], float)
    assert result["score"] >= 0.0


def test_search_company_urls_returns_list():
    """search_company_urls() always returns a list (no exceptions propagated)."""
    from agents.web_scraper import search_company_urls

    with patch("agents.web_scraper._ddgs_search_one", return_value=[]):
        result = search_company_urls("Nonexistent Corp XYZ")

    assert isinstance(result, list)


def test_parallel_faster_than_sequential():
    """Two queries run concurrently — total time should be less than sum of individual times.

    Mocks asyncio.to_thread with a 0.1s sleep and verifies 2 parallel queries
    finish in <0.15s (sequential would be ~0.2s).
    Patch target is `asyncio.to_thread` (stdlib), not `agents.web_scraper.asyncio.to_thread`.
    """
    import time

    async def _fake_to_thread(fn, *args):
        await asyncio.sleep(0.1)
        return ["https://example.com"]

    from agents import web_scraper

    with patch("asyncio.to_thread", side_effect=_fake_to_thread):
        start = time.monotonic()
        result = asyncio.run(
            web_scraper._search_company_urls_async("Test Corp", max_results=4)
        )
        elapsed = time.monotonic() - start

    # Parallel: ~0.1s. Sequential: ~0.2s.
    assert elapsed < 0.15, f"Expected parallel <0.15s, got {elapsed:.2f}s"


def test_find_best_candidate_is_async():
    """find_best_candidate must be a coroutine function (awaitable)."""
    import inspect
    from agents.web_scraper import find_best_candidate

    assert inspect.iscoroutinefunction(find_best_candidate)
