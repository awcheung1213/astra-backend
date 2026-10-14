"""What a plan step actually needs from the owner (#25 collect_study_data),
as questions he can answer on the card itself — never "go read your plan".

The Strategist names them per workstream (`client_asks`); a plan written
before that existed (or a workstream that only says needs_client_data)
falls back to defaults picked from the workstream's name. Pure functions:
the approval fan-out stores the asks on the task, the approvals feed reads
them, the answer endpoint validates against them."""

import re

KIND_NUMBER = "number"
KIND_TEXT = "text"
KINDS = (KIND_NUMBER, KIND_TEXT)
MAX_ASKS = 3
MAX_TEXT_ANSWER = 500

KEY_MONTHLY_REVENUE = "monthly_revenue"  # feeds the goal's starting point
KEY_MONTHLY_CUSTOMERS = "monthly_customers"

BASELINE_ASKS = [
    {"key": KEY_MONTHLY_REVENUE, "question": "Money coming in per month right now", "kind": KIND_NUMBER, "unit": "$ a month", "optional": False},
    {"key": KEY_MONTHLY_CUSTOMERS, "question": "New customers or jobs per month", "kind": KIND_NUMBER, "unit": "a month", "optional": True},
]
PROOF_ASKS = [
    {"key": "featured_customers", "question": "Name 2 or 3 customers we could feature", "kind": KIND_TEXT, "unit": None, "optional": True},
]


def default_asks(workstream_name: str | None) -> list[dict]:
    """Defaults by what the step is about: a baseline/measurement step needs
    today's numbers; a proof/case-study step needs customers to feature;
    anything else one plain question. Pure."""
    name = (workstream_name or "").lower()
    if re.search(r"baseline|measur|starting point|benchmark", name):
        return [dict(ask) for ask in BASELINE_ASKS]
    if re.search(r"proof|case stud|testimonial|story|stories", name):
        return [dict(ask) for ask in PROOF_ASKS]
    return [{"key": "details", "question": f"What can you share for “{workstream_name or 'this step'}”? A sentence is fine.", "kind": KIND_TEXT, "unit": None, "optional": False}]


def normalize_asks(raw: object) -> list[dict]:
    """The Strategist's client_asks, cleaned: known kinds only, a question
    present, stable unique keys, at most MAX_ASKS. Pure."""
    if not isinstance(raw, list):
        return []
    asks: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("question") or "").strip():
            continue
        kind = item.get("kind") if item.get("kind") in KINDS else KIND_TEXT
        key = re.sub(r"[^a-z0-9_]+", "_", str(item.get("key") or item["question"]).lower()).strip("_")[:40] or f"ask_{len(asks)}"
        if key in seen:
            key = f"{key}_{len(asks)}"
        seen.add(key)
        asks.append({"key": key, "question": str(item["question"]).strip(), "kind": kind, "unit": item.get("unit") or None, "optional": bool(item.get("optional", False))})
        if len(asks) == MAX_ASKS:
            break
    return asks


def workstream_asks(workstream: dict) -> list[dict]:
    """The asks a plan workstream turns into ([] when it needs nothing). Pure."""
    asks = normalize_asks(workstream.get("client_asks"))
    if asks:
        return asks
    return default_asks(workstream.get("name")) if workstream.get("needs_client_data") else []


def asks_for(payload: dict | None) -> list[dict]:
    """A collect_study_data task's questions: stored asks, else the defaults
    for its workstream (tasks born before asks existed). Pure."""
    payload = payload or {}
    return normalize_asks(payload.get("asks")) or default_asks(payload.get("workstream"))


def clean_answers(asks: list[dict], answers: dict) -> tuple[dict, list[str]]:
    """(the answers to store, problems). Numbers parse from "$12,500" or
    "12.5k"; text is trimmed and capped; unknown keys are dropped; every
    required ask must be answered. Pure."""
    cleaned: dict = {}
    problems: list[str] = []
    for ask in asks:
        raw = answers.get(ask["key"])
        if raw is None or str(raw).strip() == "":
            if not ask.get("optional"):
                problems.append(f"{ask['question']}: needs an answer")
            continue
        if ask["kind"] == KIND_NUMBER:
            number = parse_number(raw)
            if number is None or number < 0:
                problems.append(f"{ask['question']}: needs a number")
                continue
            cleaned[ask["key"]] = number
        else:
            cleaned[ask["key"]] = str(raw).strip()[:MAX_TEXT_ANSWER]
    if not cleaned and not problems:
        problems.append("Answer at least one question")
    return cleaned, problems


def parse_number(raw: object) -> float | None:
    """'$12,500' -> 12500, '12.5k' -> 12500, '1.2m' -> 1200000. Pure."""
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().lower().replace(",", "").replace("$", "").replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", text)
    if not match:
        return None
    return float(match.group(1)) * {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2)]


def answered_lines(payload: dict | None) -> list[dict]:
    """[{question, value, unit}] for a done task, in ask order — the plan
    step's "what you told us". Pure."""
    payload = payload or {}
    answers = payload.get("answers") or {}
    return [{"question": ask["question"], "value": answers[ask["key"]], "unit": ask.get("unit")} for ask in asks_for(payload) if ask["key"] in answers]
