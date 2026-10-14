"""Short question topics — the 2-4 word plain label a tracked question shows on
the AI answers tile grid ("Emergency plumber"). Research asks for one per
question; backfill_question_topics labels older rows. Validation is pure and
LENIENT on the caller: a bad label becomes None (the FE falls back to the
question's first words), it never fails the research reply around it."""

import json
import re
from typing import Any

from utils.agent_json import parse_agent_json

TOPIC_MAX_WORDS = 4
TOPIC_MAX_CHARS = 40
_EDGE_PUNCTUATION = " \t\n\"'`“”‘’.,;:!?-–—*_()[]{}"


def clean_topic(raw: Any, brand_name: str | None = None) -> str | None:
    """A model-written topic, normalized to a tile label, or None when unusable:
    not a string, empty, longer than TOPIC_MAX_WORDS words / TOPIC_MAX_CHARS
    chars, a full question, or naming the brand. First letter capitalized, the
    rest kept as written (acronyms survive). Pure."""
    if not isinstance(raw, str):
        return None
    if "\n" in raw.strip():
        return None
    topic = " ".join(raw.split()).strip(_EDGE_PUNCTUATION)
    if not topic or "?" in topic:
        return None
    if len(topic.split()) > TOPIC_MAX_WORDS or len(topic) > TOPIC_MAX_CHARS:
        return None
    if brand_name and brand_name.strip() and re.search(rf"\b{re.escape(brand_name.strip())}\b", topic, re.IGNORECASE):
        return None
    return topic[0].upper() + topic[1:]


def topic_backfill_prompt(questions: list[str]) -> str:
    """The backfill's task prompt: questions numbered from 1 (the reply keys on n)."""
    numbered = "\n".join(f"{index}. {question}" for index, question in enumerate(questions, start=1))
    return f"Label each of these {len(questions)} consumer questions:\n\n{numbered}"


def parse_topic_reply(agent_output: str, question_count: int, brand_name: str | None = None) -> dict[int, str]:
    """The backfill reply ([{"n": 1, "topic": "..."}, ...]) -> {n: clean topic}.
    Entries with an out-of-range n, a duplicate n, or an unusable topic are
    dropped (that question stays NULL for a later run). ValueError when the
    reply is not a JSON array at all, so procrastinate retries. Pure."""
    parsed = parse_agent_json(agent_output)
    if not isinstance(parsed, list):
        raise ValueError(f"topic backfill output is not a JSON array: {json.dumps(parsed)[:200]}")
    topics: dict[int, str] = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        number = entry.get("n")
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= question_count or number in topics:
            continue
        topic = clean_topic(entry.get("topic"), brand_name)
        if topic is not None:
            topics[number] = topic
    return topics


# STATIC on purpose (cache_control): the questions ride the task prompt.
TOPIC_BACKFILL_SYSTEM = """You label consumer questions that people ask AI assistants, for a small dashboard tile.

For each numbered question write a topic: 2 to 4 plain words naming what the person is looking for, e.g. "Emergency plumber", "Water heater repair", "Kids birthday cakes".

HARD RULES:
- No brand, business, or company names. No city or place names.
- No jargon or marketing words; write it the way the customer would say it.
- A short noun phrase, not a question, no punctuation.

Respond with ONLY a JSON array, no prose and no code fences, one entry per question:
[{"n": 1, "topic": "Emergency plumber"}]"""
