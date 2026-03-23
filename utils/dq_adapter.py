"""DQ adapter — route LLM calls through openrouter_wrapper with claude -p fallback.

Tier 1 = Ollama local (qwen2.5-coder)
Tier 2 = Groq free (llama-3.3-70b) — default for extraction and DAFO
Tier 3 = Anthropic Sonnet — for complex narrative generation
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_DQ_CORE = Path("/root/jarvis/bin/core")
_CLAUDE_TIMEOUT = 120
_CLAUDE_MODEL = "claude-sonnet-4-6"  # same default as settings.claude_model
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def call_llm(prompt: str, tier: int = 2) -> str:
    """Send prompt to LLM, returning response text.

    Args:
        prompt: The full prompt string.
        tier: DQ routing tier (1=local, 2=Groq free, 3=Sonnet paid).

    Returns:
        Response text, or "" on any failure.
    """
    try:
        if _DQ_CORE.exists():
            sys.path.insert(0, str(_DQ_CORE))
            from openrouter_wrapper import process_prompt  # noqa: PLC0415

            result = process_prompt(prompt, tier_override=tier)
            response = result.get("response") or ""
            if response:
                return response.strip()
    except Exception:
        pass

    # Fallback: claude -p headless (preserves --model and cwd from original run_claude)
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", _CLAUDE_MODEL],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_CLAUDE_TIMEOUT,
            cwd=str(_PROJECT_ROOT),
        )
        return result.stdout.strip()
    except Exception:
        return ""
