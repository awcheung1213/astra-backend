"""Parsing strict-JSON agent replies (agents prompted "JSON only, no prose")."""

import json
from typing import Any


def parse_agent_json(agent_output: str) -> Any:
    """The parsed JSON, tolerating a stray markdown code fence around it.
    ValueError when the output is not JSON at all — callers raise onward so
    procrastinate retries (a fresh call usually complies)."""
    body = agent_output.strip()
    if body.startswith("```"):
        body = body.strip("`\n")
        body = body.removeprefix("json")
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise ValueError(f"agent output is not JSON: {agent_output[:200]}") from error
