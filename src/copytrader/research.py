"""Headless Claude Code research for prediction-market events.

Shells out to the local `claude` CLI in `-p` mode (non-interactive) with web
search enabled. Uses the user's existing Claude Pro/Max subscription auth — no
ANTHROPIC_API_KEY required, no per-call billing.

Returns a YES-side probability estimate that gets blended into the bet
recommendation via analyze.recommend(research_prob=...).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "prob_yes": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reasoning": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["prob_yes", "confidence", "reasoning"],
}


@dataclass
class ResearchResult:
    prob_yes: float
    confidence: str
    reasoning: str
    sources: list[str]


def _build_prompt(title: str, end_date: str) -> str:
    return (
        f"Estimate the probability of the YES outcome for this Polymarket market. "
        f"For 'Team A vs. Team B' markets, YES = Team A wins. "
        f"Search the web for recent news, lineups, injuries, expert predictions, and "
        f"live game state if applicable. Reply with ONLY a JSON object — no preamble, "
        f"no chat.\n\n"
        f"Market: {title}\n"
        f"Resolves by: {end_date}\n\n"
        'Format: {"prob_yes": <0..1>, "confidence": "low|medium|high", '
        '"reasoning": "1-3 sentences", "sources": ["url1","url2"]}\n\n'
        f"Be calibrated. If you cannot find useful info, return prob_yes=0.5 with "
        f"low confidence. Do not assume the market price — produce an independent estimate."
    )


def is_available() -> bool:
    """True if the local `claude` CLI is on PATH."""
    return shutil.which("claude") is not None


def research_market(
    title: str,
    end_date: str,
    *,
    timeout_sec: int = 180,
) -> ResearchResult | None:
    """Run Claude Code headlessly to research a market. Returns None on failure."""
    claude_path = shutil.which("claude")
    if claude_path is None:
        log.warning("claude CLI not found on PATH; skipping research")
        return None

    # Pass the prompt via stdin to avoid Windows CMD arg-quoting issues with
    # special chars ({, }, ", etc.) in the prompt text.
    cmd = [
        claude_path,
        "-p",
        "--allowedTools", "WebSearch", "WebFetch",
        "--effort", "medium",
        "--exclude-dynamic-system-prompt-sections",
    ]
    try:
        result = subprocess.run(
            cmd, input=_build_prompt(title, end_date),
            capture_output=True, text=True, timeout=timeout_sec, check=False,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        log.warning("claude research timed out (%ds) for %s", timeout_sec, title[:60])
        return None
    except FileNotFoundError:
        log.warning("claude CLI not invocable; skipping research")
        return None

    if result.returncode != 0:
        log.warning("claude research exited %d: %s", result.returncode, (result.stderr or "")[:300])
        return None

    raw = (result.stdout or "").strip()
    if not raw:
        log.warning("claude research returned empty stdout")
        return None

    # The CLI may surround the JSON with extra text; try to extract.
    payload = _extract_json(raw)
    if payload is None:
        log.warning("could not extract JSON from claude output (first 200 chars): %r", raw[:200])
        return None

    try:
        return ResearchResult(
            prob_yes=float(payload["prob_yes"]),
            confidence=str(payload.get("confidence", "low")),
            reasoning=str(payload.get("reasoning", ""))[:500],
            sources=[str(s) for s in (payload.get("sources") or [])][:10],
        )
    except (KeyError, TypeError, ValueError) as e:
        log.warning("invalid research payload: %s", e)
        return None


def _extract_json(text: str) -> dict | None:
    """Try to load the whole text as JSON, else find the first {...} block."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
