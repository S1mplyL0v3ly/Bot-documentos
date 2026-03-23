"""Tests for utils/dq_adapter.py — DQ routing with claude -p fallback."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_call_llm_returns_string():
    """call_llm() must return a string in all cases."""
    from utils.dq_adapter import call_llm

    # Patch subprocess so we don't need claude installed
    with patch("utils.dq_adapter.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="test response", returncode=0)
        result = call_llm("test prompt")
    assert isinstance(result, str)


def test_call_llm_uses_openrouter_when_available():
    """call_llm() returns openrouter_wrapper response when DQ path is available."""
    mock_module = MagicMock()
    mock_module.process_prompt.return_value = {"response": "groq_response"}

    from utils import dq_adapter

    orig = dq_adapter._DQ_CORE
    # Point _DQ_CORE at /tmp (exists) so the DQ branch is entered
    dq_adapter._DQ_CORE = Path("/tmp")
    try:
        with patch.dict(sys.modules, {"openrouter_wrapper": mock_module}):
            result = dq_adapter.call_llm("hello", tier=2)
    finally:
        dq_adapter._DQ_CORE = orig

    # Must have returned the Groq response, not the subprocess fallback
    assert result == "groq_response"
    mock_module.process_prompt.assert_called_once_with("hello", tier_override=2)


def test_call_llm_falls_back_to_claude_on_import_error():
    """When openrouter_wrapper is unavailable, falls back to subprocess claude -p."""
    from unittest.mock import patch, MagicMock
    from utils.dq_adapter import call_llm

    with patch("utils.dq_adapter.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="fallback_response", returncode=0)
        # Make DQ_CORE not exist so openrouter branch is skipped
        from utils import dq_adapter

        orig = dq_adapter._DQ_CORE
        dq_adapter._DQ_CORE = Path("/nonexistent_path_xyz")
        try:
            result = call_llm("test prompt")
        finally:
            dq_adapter._DQ_CORE = orig

    assert result == "fallback_response"


def test_call_llm_returns_empty_on_total_failure():
    """Returns '' if both DQ and claude -p fail."""
    from utils.dq_adapter import call_llm

    with patch("utils.dq_adapter.subprocess.run", side_effect=Exception("fail")):
        from utils import dq_adapter

        orig = dq_adapter._DQ_CORE
        dq_adapter._DQ_CORE = Path("/nonexistent_path_xyz")
        try:
            result = call_llm("test prompt")
        finally:
            dq_adapter._DQ_CORE = orig

    assert result == ""
