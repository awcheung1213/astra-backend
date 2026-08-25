from dataclasses import dataclass


@dataclass
class CitationContext:
    cited_text: str
    title: str
    type: str
    url: str
    # real source domain when the provider proxies its citation urls (gemini
    # grounding returns vertexaisearch.cloud.google.com redirect links); None
    # means "derive it from the url host"
    domain: str | None = None


@dataclass
class LocationContext:
    city: str
    state: str
    # system markets are seeded without a zip; city + state is enough context
    zip: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass
class QueryRequest:
    custom_id: str
    prompt: str
    location: LocationContext | None = None
    system: str | None = None
    schema: dict | None = None
    use_web_search: bool | None = None
    # cache the (static) system block so repeated instructions bill at 10%
    cache_system: bool = False


@dataclass
class QueryResult:
    custom_id: str
    text: str
    raw: dict | None = None
    citations: list[CitationContext] | None = None
    structured: dict | None = None
    usage: dict | None = None
    error: str | None = None


@dataclass
class BatchHandle:
    provider: str
    batch_id: str
    status: str
    extra: dict | None = None
