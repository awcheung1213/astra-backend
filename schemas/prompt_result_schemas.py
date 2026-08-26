from dataclasses import dataclass
from datetime import datetime


@dataclass
class CompetitorMention:
    id: str
    competitor_id: str
    position: int | None
    created_at: datetime


@dataclass
class PromptResultWithCompetitorMentions:
    id: str
    prompt_id: str
    market_id: str
    brand_id: str
    engine: str
    text: str
    raw: str
    brand_cited: bool
    brand_mentioned: bool
    brand_mention_position: int | None
    positive_sentiment: bool | None
    top_3_position: bool | None
    competitor_mention_count: int | None
    query_response_id: str | None
    created_at: datetime
    competitor_mentions: list[CompetitorMention]
