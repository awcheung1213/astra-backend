"""Deterministic competitor detection: match the brand's known competitor
names against AI answer text. Free per response - the LLM is only needed to
DISCOVER names that aren't in the brand's list yet."""

import re
from dataclasses import dataclass

from objects.competitor import Competitor
from utils.response_parsing import parse_prompt_result_for_mention_rank


@dataclass
class CompetitorRegexPatterns:
    patterns: dict[str, re.Pattern]


def build_competitor_regex_patterns(competitors: list["Competitor"]) -> CompetitorRegexPatterns:
    r"""generate a dictionary of competitor name -> competitor regex pattern
    example: "Acme Inc." -> re.compile(r"\bAcme\ Inc\.\b", re.IGNORECASE)
    parse_prompt_result_for_competitor_mention_rank() uses this object to search AI text for matching competitor patterns.
    """
    patterns: dict[str, re.Pattern] = {}
    for competitor in competitors:
        name = (competitor.name or "").strip()
        if not name:
            continue
        patterns[competitor.name] = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
    return CompetitorRegexPatterns(patterns=patterns)


def parse_prompt_result_for_competitor_mention_rank(text: str, competitor_regex_patterns: CompetitorRegexPatterns, exclude: str | None = None) -> list[tuple[str, int | None]]:
    competitors_mentioned = [name for name, pattern in competitor_regex_patterns.patterns.items() if pattern.search(text)]
    if exclude:
        brand_name_to_exclude = exclude.strip().lower()
        competitors_mentioned = [name for name in competitors_mentioned if name.strip().lower() != brand_name_to_exclude]
    if not competitors_mentioned:
        return []
    competitor_mention_ranks = parse_prompt_result_for_mention_rank(text, competitors_mentioned)
    return [(name, competitor_mention_ranks.get(name)) for name in competitors_mentioned]
