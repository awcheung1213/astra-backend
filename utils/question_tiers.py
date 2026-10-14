"""Question levels — the automatic core / watching / suggested funnel
(owner-approved "Lean", 2026-10-14). The owner never manages a list: we pick,
check and move the questions; he can pin one, stop one, or add his own.

  core       CORE_SLOTS questions, weekly, all assistants, the full panel —
             the AI answers tiles.
  watching   the long tail, checked in a rotation of LONGTAIL_CHECKS_PER_MONTH
             checks per brand per month on the small panel, spread evenly
             over the month's collection weeks (rotation_slice).
  suggested  discovered, not checked — the unlimited pool research keeps
             adding to (monthly discovery cron + roadmap approvals).

CONTRACT used by worker/tasks/collection.py: `questions_to_collect(brand_id,
now)` -> CollectionPlan(core_ids, longtail_ids). It is the ONLY place
collection gets its question list.

The weekly sorter (worker/tasks/question_sorter.py, Sunday after scoring)
applies plan_moves:

  PROMOTE watching -> core when the question is worth fighting for:
    competitor  a competitor is named in a majority of the question's answers
                while no assistant names the brand
    named       the brand starts being named (share > 0 on any assistant)
    demand      real Google search demand: a Search Console query (when
                connected) whose words all appear in the question
  DEMOTE core -> watching:
    won         named on every active assistant for STEADY_WEEKS (8) weeks running
    dead        no brand and no competitor named for STEADY_WEEKS weeks running
    room        the lowest-value movable core question, when a promotion
                needs its slot (only if the newcomer is worth more)
  GUARDS: a promotion needs its signal on CONSECUTIVE_CHECKS (2) checks in a
  row (demand: in both halves of the 28-day Search Console window); at most
  MAX_AUTO_SWAPS_PER_MONTH (2) automatic swaps (a demotion out of core) per
  brand per calendar month; pinned (owner-set) and owner-written questions
  are never moved automatically; a question that came into core less than
  MIN_CORE_WEEKS ago is never the one bumped to make room.
  OPEN SPOTS: a core with fewer than CORE_SLOTS questions is topped up from
  watching (signal first, then value, then research rank) — not a swap.
  LONG-TAIL UPKEEP (keeps discovery flowing into the rotation): a watching
  question whose last 2 checks named no business at all goes back to the
  pool (suggested); watching is topped up to WATCHING_POOL_TARGET from
  never-moved suggested questions, newest research first. Neither counts
  as a swap. A question the owner stopped (or one sent back) is never
  re-picked automatically (only never-moved suggestions are).

VALUE (the "worth fighting for" score, latest check):
    2 x [competitor named in a majority, brand named nowhere]
  + 1 x [brand named somewhere but not on every assistant]  (winnable)
  + 1 x [Search Console demand]
  + the top competitor's answer share (0..1, a tie-breaker)
A won question (named everywhere) or a dead one (nothing named) scores ~0.

Everything except questions_to_collect / the assignment writers is pure
(tests/test_question_tiers.py)."""

import math
import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from objects.prompt import SOURCE_USER, TIER_CORE, TIER_SUGGESTED, TIER_WATCHING, TIERS, Prompt  # noqa: F401  (TIERS re-exported)
from objects.question_move import QuestionMove
from utils import answer_stats

CORE_SLOTS = 5
LONGTAIL_CHECKS_PER_MONTH = 30
WATCHING_POOL_TARGET = 40  # the rotation's pool: ~30 checks a month -> each question about every 5-6 weeks
MAX_AUTO_SWAPS_PER_MONTH = 2
CONSECUTIVE_CHECKS = 2
STEADY_WEEKS = 8
MIN_CORE_WEEKS = 2
PROMISING_SHARE = 3  # 1 in PROMISING_SHARE of a week's rotation slots re-checks questions with a pending signal
COMPETITOR_MAJORITY = 0.5  # a competitor named in MORE than this share of the answers

SIGNAL_COMPETITOR = "competitor"
SIGNAL_NAMED = "named"
SIGNAL_DEMAND = "demand"
PROMOTION_PRIORITY = {SIGNAL_COMPETITOR: 0, SIGNAL_DEMAND: 1, SIGNAL_NAMED: 2}

# reason code -> the plain words the owner sees (first words carry the point)
REASON_PROMOTED_COMPETITOR = "promoted_competitor"
REASON_PROMOTED_NAMED = "promoted_named"
REASON_PROMOTED_DEMAND = "promoted_demand"
REASON_FILLED_OPEN = "filled_open"
REASON_DEMOTED_WON = "demoted_won"
REASON_DEMOTED_DEAD = "demoted_dead"
REASON_DEMOTED_ROOM = "demoted_room"
REASON_WATCHING_ADDED = "watching_added"
REASON_WATCHING_QUIET = "watching_quiet"
REASON_INITIAL_CORE = "initial_core"
REASON_INITIAL_WATCHING = "initial_watching"
REASON_OWNER_MOVED = "owner_moved"
REASON_OWNER_STOPPED = "owner_stopped"
REASON_OWNER_ADDED = "owner_added"
REASON_OWNER_EDITED = "owner_edited"
REASON_MADE_ROOM_FOR_OWNER = "made_room_for_owner"
REASONS = {
    REASON_PROMOTED_COMPETITOR: "Competitors get named here and you don't yet",
    REASON_PROMOTED_NAMED: "AI assistants started naming you here",
    REASON_PROMOTED_DEMAND: "People search Google for this",
    REASON_FILLED_OPEN: "Moved up to fill an open weekly spot",
    REASON_DEMOTED_WON: "Every AI assistant has named you here for 8 weeks",
    REASON_DEMOTED_DEAD: "No business named here for 8 weeks",
    REASON_DEMOTED_ROOM: "Made room for a question worth more",
    REASON_WATCHING_ADDED: "New question we started watching",
    REASON_WATCHING_QUIET: "AI names no businesses for this one",
    REASON_INITIAL_CORE: "One of the questions customers ask most",
    REASON_INITIAL_WATCHING: "Another common question, checked monthly",
    REASON_OWNER_MOVED: "You picked this",
    REASON_OWNER_STOPPED: "You stopped this one",
    REASON_OWNER_ADDED: "You added this question",
    REASON_OWNER_EDITED: "You reworded this question",
    REASON_MADE_ROOM_FOR_OWNER: "Made room for the question you picked",
}
SIGNAL_REASON = {SIGNAL_COMPETITOR: REASON_PROMOTED_COMPETITOR, SIGNAL_NAMED: REASON_PROMOTED_NAMED, SIGNAL_DEMAND: REASON_PROMOTED_DEMAND}


@dataclass
class CollectionPlan:
    core_ids: list[str] = field(default_factory=list)  # weekly, full panel, web search on
    longtail_ids: list[str] = field(default_factory=list)  # this run's slice of the rotation: small panel, no web search


##############################
# COLLECTION CONTRACT
##############################


async def questions_to_collect(brand_id: str, now: datetime, record: bool = True) -> CollectionPlan:
    """Which of the brand's questions to collect this run: every core question
    + this week's slice of the watching rotation (rotation_slice). With
    `record` (the default) it stamps last_checked_at on the core questions and
    on the slice's NEW picks, in the caller's session — so collection commits
    the stamps with its batches. Idempotent within a week: a second call in
    the same week returns the slice already picked (topped up only if the
    week's target grew), never a second slice. record=False = a read-only
    preview (stamps nothing)."""
    prompts = await Prompt.by_brand(brand_id)
    core = sorted((prompt for prompt in prompts if prompt.tier == TIER_CORE), key=rank_key)
    watching = [prompt for prompt in prompts if prompt.tier == TIER_WATCHING]
    this_week, new_picks = rotation_slice(watching, now)
    if record:
        await Prompt.mark_checked(brand_id, [prompt.id for prompt in core] + new_picks, now)
    return CollectionPlan(core_ids=[prompt.id for prompt in core], longtail_ids=this_week)


##############################
# PURE: RANK + ROTATION
##############################


def rank_key(prompt: Any) -> tuple:
    """Research rank: research stamps its reply order (most-asked first) into
    created_at, 1µs apart; ties keep id order."""
    return (getattr(prompt, "created_at", None) or datetime.max, getattr(prompt, "id", ""))


def discovery_key(prompt: Any) -> tuple:
    """Newest research run first (runs are minutes+ apart), rank order inside a run."""
    created_at = getattr(prompt, "created_at", None) or datetime.min
    return (-created_at.replace(second=0, microsecond=0).timestamp(), created_at, getattr(prompt, "id", ""))


def month_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, 1)


def week_start(now: datetime) -> datetime:
    """Monday 00:00 of now's week — the rotation's "this week"."""
    monday = now.date() - timedelta(days=now.weekday())
    return datetime(monday.year, monday.month, monday.day)


def weeks_left_in_month(now: datetime) -> int:
    """Collection weeks left in this month, this one included = how many more
    times today's weekday occurs (the weekly collection runs on one weekday)."""
    last_day = monthrange(now.year, now.month)[1]
    return math.ceil((last_day - now.day + 1) / 7)


def slice_size(now: datetime, checked_earlier_this_month: int) -> int:
    """This week's share of the month's long-tail checks: what's left of
    LONGTAIL_CHECKS_PER_MONTH spread evenly over the weeks left (rounded up,
    so the month's total is reached by its last collection week)."""
    remaining = max(0, LONGTAIL_CHECKS_PER_MONTH - checked_earlier_this_month)
    return math.ceil(remaining / weeks_left_in_month(now))


def rotation_slice(watching: list[Any], now: datetime) -> tuple[list[str], list[str]]:
    """(this week's slice ids, the NEW picks among them). Already picked this
    week (last_checked_at since Monday, or since the 1st if later) stay this
    week's; the week's target is slice_size over the checks made earlier this
    month; the shortfall is picked as: 1 in PROMISING_SHARE slots (at least
    one) for questions with a pending signal (strongest first: competitor,
    demand, named), the rest never-checked first, then oldest-checked, then
    research rank. Pure."""
    start_of_month = month_start(now)
    start_of_week = max(week_start(now), start_of_month)
    already = [prompt for prompt in watching if prompt.last_checked_at is not None and prompt.last_checked_at >= start_of_week]
    earlier = sum(1 for prompt in watching if prompt.last_checked_at is not None and start_of_month <= prompt.last_checked_at < start_of_week)
    need = max(0, slice_size(now, earlier) - len(already))
    taken = {prompt.id for prompt in already}
    candidates = [prompt for prompt in watching if prompt.id not in taken]
    picks: list[str] = []
    if need:
        promising = sorted((prompt for prompt in candidates if getattr(prompt, "pending_signal", None)), key=lambda prompt: (PROMOTION_PRIORITY.get(prompt.pending_signal, 9), prompt.last_checked_at or datetime.min, rank_key(prompt)))
        for prompt in promising[: max(1, need // PROMISING_SHARE)]:
            picks.append(prompt.id)
        chosen = set(picks)
        oldest_first = sorted((prompt for prompt in candidates if prompt.id not in chosen), key=lambda prompt: (prompt.last_checked_at is not None, prompt.last_checked_at or datetime.min, rank_key(prompt)))
        picks += [prompt.id for prompt in oldest_first[: need - len(picks)]]
    return [prompt.id for prompt in sorted(already, key=rank_key)] + picks, picks


##############################
# PURE: INITIAL ASSIGNMENT
##############################


def initial_tiers(prompts: list[Any]) -> dict[str, str]:
    """Launch's default levels: nothing when any live question already has a
    level (core/watching) — the owner or an earlier default chose; otherwise
    the top CORE_SLOTS by research rank -> core, the next
    WATCHING_POOL_TARGET -> watching. {prompt_id: tier}, rank order. Pure."""
    live = [prompt for prompt in prompts if getattr(prompt, "retired_at", None) is None]
    if any(getattr(prompt, "tier", TIER_SUGGESTED) in (TIER_CORE, TIER_WATCHING) for prompt in live):
        return {}
    ranked = sorted(live, key=rank_key)
    tiers = {prompt.id: TIER_CORE for prompt in ranked[:CORE_SLOTS]}
    tiers.update({prompt.id: TIER_WATCHING for prompt in ranked[CORE_SLOTS:][:WATCHING_POOL_TARGET]})
    return tiers


##############################
# PURE: CHECKS + SIGNALS
##############################


@dataclass
class CheckSummary:
    """One question's answers in one week (one "check")."""

    engines: dict[str, dict]  # answer_stats.engine_stats per engine (dead-zone named)
    answers: int = 0  # answers read (newest per market x engine)
    top_competitor_share: float = 0.0  # the most-named competitor's share of those answers
    any_competitor: bool = False

    @property
    def named_count(self) -> int:
        return sum(1 for stats in self.engines.values() if stats.get("named"))

    @property
    def brand_any(self) -> bool:
        return any((stats.get("cities_named") or 0) > 0 for stats in self.engines.values())

    @property
    def named_everywhere(self) -> bool:
        return bool(self.engines) and all(stats.get("named") for stats in self.engines.values())


def check_summary(engine_stats_by_engine: dict[str, dict], answers: list[Any], mentions_by_result_id: dict[str, list[Any]]) -> CheckSummary:
    """Build one check from that week's per-engine stats and the answers read
    (already one per market x engine). Pure."""
    counts: dict[str, int] = {}
    for answer in answers:
        for name in {mention.normalized_name for mention in mentions_by_result_id.get(answer.id, [])}:
            counts[name] = counts.get(name, 0) + 1
    top = max(counts.values(), default=0)
    return CheckSummary(engines=engine_stats_by_engine, answers=len(answers), top_competitor_share=top / len(answers) if answers else 0.0, any_competitor=bool(counts))


def check_signals(check: CheckSummary) -> set[str]:
    """The answer-derived promotion signals one check carries."""
    signals = set()
    if check.answers and check.top_competitor_share > COMPETITOR_MAJORITY and check.named_count == 0:
        signals.add(SIGNAL_COMPETITOR)
    if check.brand_any:
        signals.add(SIGNAL_NAMED)
    return signals


def promotion_signal(checks: list[CheckSummary], demand_recent: bool = False, demand_prior: bool = False) -> str | None:
    """The CONFIRMED promotion signal (checks newest first): an answer signal
    on the last CONSECUTIVE_CHECKS checks, or demand in both Search Console
    halves. Strongest first: competitor, demand, named. None = no move."""
    confirmed: set[str] = set()
    if len(checks) >= CONSECUTIVE_CHECKS:
        confirmed = set.intersection(*(check_signals(check) for check in checks[:CONSECUTIVE_CHECKS]))
    if demand_recent and demand_prior:
        confirmed.add(SIGNAL_DEMAND)
    return min(confirmed, key=lambda signal: PROMOTION_PRIORITY[signal]) if confirmed else None


def pending_signal(checks: list[CheckSummary], demand_recent: bool = False) -> str | None:
    """A signal seen on the latest check (or demand in the recent half) that
    isn't confirmed yet — the rotation re-checks these first."""
    seen = check_signals(checks[0]) if checks else set()
    if demand_recent:
        seen.add(SIGNAL_DEMAND)
    return min(seen, key=lambda signal: PROMOTION_PRIORITY[signal]) if seen else None


def demotion_signal(weekly_checks: list[CheckSummary | None]) -> str | None:
    """won / dead for a CORE question: the last STEADY_WEEKS weekly checks
    (index 0 = this week; None = not asked that week, which breaks the run)."""
    window = weekly_checks[:STEADY_WEEKS]
    if len(window) < STEADY_WEEKS or any(check is None for check in window):
        return None
    if all(check.named_everywhere for check in window if check is not None):
        return REASON_DEMOTED_WON
    if all(not check.brand_any and not check.any_competitor for check in window if check is not None):
        return REASON_DEMOTED_DEAD
    return None


def is_quiet(checks: list[CheckSummary]) -> bool:
    """A watching question whose last CONSECUTIVE_CHECKS checks named no business at all."""
    return len(checks) >= CONSECUTIVE_CHECKS and all(not check.brand_any and not check.any_competitor for check in checks[:CONSECUTIVE_CHECKS])


def question_value(check: CheckSummary | None, demand: bool = False) -> float | None:
    """The worth-fighting-for score (module doc). None = never checked."""
    if check is None:
        return 1.0 if demand else None
    value = check.top_competitor_share
    if SIGNAL_COMPETITOR in check_signals(check):
        value += 2.0
    if check.brand_any and not check.named_everywhere:
        value += 1.0
    if demand:
        value += 1.0
    return round(value, 4)


_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"the", "and", "for", "near", "me", "my", "in", "a", "an", "of", "to", "is", "are", "what", "who", "how", "best", "top", "with", "on", "at", "or", "do", "does", "can", "i", "you", "your"}


def _content_words(text: str) -> set[str]:
    return {word[:-1] if len(word) > 3 and word.endswith("s") else word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS and len(word) >= 3}


def demand_match(question_text: str, queries: Iterable[str]) -> bool:
    """A Search Console query backs the question when the query has at least
    two content words and every one appears in the question (plurals folded). Pure."""
    question_words = _content_words(question_text)
    for query in queries:
        query_words = _content_words(query)
        if len(query_words) >= 2 and query_words <= question_words:
            return True
    return False


##############################
# PURE: THE WEEKLY MOVES
##############################


@dataclass
class QuestionState:
    id: str
    tier: str
    pinned: bool = False
    owner_written: bool = False
    created_at: datetime | None = None
    tier_changed_at: datetime | None = None
    promotion: str | None = None  # confirmed promotion signal (watching)
    demotion: str | None = None  # REASON_DEMOTED_WON | REASON_DEMOTED_DEAD (core)
    quiet: bool = False  # watching: last 2 checks named no business
    value: float | None = None  # None = never checked

    @property
    def movable(self) -> bool:
        return not self.pinned and not self.owner_written


@dataclass
class Move:
    prompt_id: str
    from_tier: str
    to_tier: str
    reason_code: str

    @property
    def reason(self) -> str:
        return REASONS[self.reason_code]

    @property
    def is_swap(self) -> bool:
        """An automatic move OUT of core — what the monthly budget counts."""
        return self.from_tier == TIER_CORE


def swaps_used(moves: Iterable[Any]) -> int:
    """Automatic swaps already made (moves recorded this month): automatic demotions out of core."""
    return sum(1 for move in moves if move.automatic and move.from_tier == TIER_CORE)


def plan_moves(questions: list[QuestionState], now: datetime, swaps_already: int = 0) -> list[Move]:
    """This week's automatic moves (module doc), in apply order. Pure."""
    by_id = {question.id: question for question in questions}
    moves: list[Move] = []
    moved: set[str] = set()
    budget = MAX_AUTO_SWAPS_PER_MONTH - swaps_already

    def move(question: QuestionState, to_tier: str, reason_code: str) -> None:
        moves.append(Move(question.id, question.tier, to_tier, reason_code))
        question.tier = to_tier
        moved.add(question.id)

    def in_tier(tier: str) -> list[QuestionState]:
        return [question for question in by_id.values() if question.tier == tier]

    def rank(question: QuestionState) -> tuple:
        return (question.created_at or datetime.max, question.id)

    # long-tail upkeep: quiet watching questions go back to the pool
    for question in sorted(in_tier(TIER_WATCHING), key=rank):
        if question.movable and question.quiet and question.promotion is None:
            move(question, TIER_SUGGESTED, REASON_WATCHING_QUIET)

    def candidates() -> list[QuestionState]:
        return sorted((question for question in in_tier(TIER_WATCHING) if question.movable and question.promotion and question.id not in moved), key=lambda question: (PROMOTION_PRIORITY[question.promotion or ""], -(question.value or 0.0), rank(question)))

    def refills() -> list[QuestionState]:
        return sorted((question for question in in_tier(TIER_WATCHING) if question.movable and question.id not in moved), key=lambda question: (question.promotion is None, -(question.value or 0.0), rank(question)))

    def won_or_dead() -> list[QuestionState]:
        return sorted((question for question in in_tier(TIER_CORE) if question.movable and question.demotion and question.id not in moved), key=rank)

    def lowest_value_core() -> QuestionState | None:
        settled = [question for question in in_tier(TIER_CORE) if question.movable and question.id not in moved and question.value is not None and (question.tier_changed_at is None or now - question.tier_changed_at >= timedelta(weeks=MIN_CORE_WEEKS))]
        if not settled:
            return None
        return min(settled, key=lambda question: (question.value or 0.0, -(question.created_at or datetime.min).timestamp(), question.id))

    while True:
        open_spots = CORE_SLOTS - len(in_tier(TIER_CORE))
        if open_spots > 0:
            pick = next(iter(candidates()), None) or next(iter(refills()), None)
            if pick is None:
                break
            move(pick, TIER_CORE, SIGNAL_REASON[pick.promotion] if pick.promotion else REASON_FILLED_OPEN)
            continue
        candidate = next(iter(candidates()), None)
        if candidate is not None and budget > 0:
            out = next(iter(won_or_dead()), None)
            if out is not None:
                move(out, TIER_WATCHING, out.demotion or REASON_DEMOTED_ROOM)
            else:
                low = lowest_value_core()
                if low is None or (low.value or 0.0) >= (candidate.value or 0.0):
                    break
                move(low, TIER_WATCHING, REASON_DEMOTED_ROOM)
            move(candidate, TIER_CORE, SIGNAL_REASON[candidate.promotion or SIGNAL_NAMED])
            budget -= 1
            continue
        out = next(iter(won_or_dead()), None)
        refill = next(iter(refills()), None)
        if out is not None and refill is not None and budget > 0:
            move(out, TIER_WATCHING, out.demotion or REASON_DEMOTED_ROOM)
            move(refill, TIER_CORE, SIGNAL_REASON[refill.promotion] if refill.promotion else REASON_FILLED_OPEN)
            budget -= 1
            continue
        break

    # long-tail upkeep: top the rotation pool up from never-moved suggestions
    fresh = sorted((question for question in in_tier(TIER_SUGGESTED) if question.movable and question.tier_changed_at is None and question.id not in moved), key=lambda question: discovery_key(question))
    for question in fresh[: max(0, WATCHING_POOL_TARGET - len(in_tier(TIER_WATCHING)))]:
        move(question, TIER_WATCHING, REASON_WATCHING_ADDED)
    return moves


##############################
# PURE: OWNER OVERRIDES
##############################


QUESTION_MIN_CHARS = 5
QUESTION_MAX_CHARS = 300


class QuestionTextError(ValueError):
    """A plain-words reason an owner's question can't be used (the API's 422)."""


_ENTITY_SUFFIXES = {"co", "corp", "inc", "ltd", "llc", "company", "corporation", "incorporated", "limited"}  # = Competitor.normalize_name's


def _name_variants(name: str) -> list[list[str]]:
    """A business name as word lists: as written, and without a leading "the"
    / trailing entity suffixes (LLC, Inc...) — how people actually type it."""
    words = [word for word in re.split(r"[\W_]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", name.strip())) if word]
    short = list(words)
    if short and short[0].lower() == "the":
        short = short[1:]
    while short and short[-1].lower() in _ENTITY_SUFFIXES:
        short.pop()
    variants = [words, short, [word for word in short if word != "&"]]
    return [variant for index, variant in enumerate(variants) if variant and variant not in variants[:index]]


def strip_business_names(text: str, names: Iterable[str]) -> str:
    """The question with business names taken out (whole words, case-
    insensitive, possessive included, any punctuation between the words;
    spacing tidied), so an owner-written question stays brandless like
    research's. Names shorter than 3 characters are ignored (too easy to hit
    a real word). Pure."""
    cleaned = text
    variants = [variant for name in names if name and name.strip() for variant in _name_variants(name)]
    for words in sorted(variants, key=lambda variant: len(" ".join(variant)), reverse=True):
        if len(" ".join(words)) < 3:
            continue
        pattern = r"\b(?:the\s+)?" + r"[\W_]+".join(re.escape(word) for word in words) + r"(?:['’]s)?\b"
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+([,.?!;:])", r"\1", cleaned)
    cleaned = re.sub(r"([,;:])\s*(?=[,.?!;:])", "", cleaned)
    return " ".join(cleaned.split()).strip(" ,;:-")


def clean_owner_question(text: str, business_names: Iterable[str]) -> str:
    """Validate + tidy an owner-written question: whitespace collapsed,
    QUESTION_MIN_CHARS..QUESTION_MAX_CHARS characters, business names taken
    out, first letter capitalized. QuestionTextError (plain words) otherwise. Pure."""
    tidy = " ".join((text or "").split())
    if len(tidy) < QUESTION_MIN_CHARS:
        raise QuestionTextError("Type a few more words.")
    if len(tidy) > QUESTION_MAX_CHARS:
        raise QuestionTextError(f"Keep it under {QUESTION_MAX_CHARS} characters.")
    stripped = strip_business_names(tidy, business_names)
    if len(re.sub(r"[\W_]+", "", stripped)) < QUESTION_MIN_CHARS - 1:
        raise QuestionTextError("Ask it the way a customer would, without a business name.")
    return stripped[0].upper() + stripped[1:]


def owner_tier_change(current_tier: str, current_pinned: bool, tier: str | None, pinned: bool | None) -> tuple[str, bool]:
    """(tier, pinned) after an owner PATCH: setting core/watching pins it
    (unless pinned=false is sent explicitly); suggested stops it — unpinned
    unless pinned is sent (the app sends pinned=true, so the funnel never
    picks a stopped question back up); pinned alone just (un)pins. Pure."""
    if tier is None:
        return current_tier, current_pinned if pinned is None else pinned
    if tier in (TIER_CORE, TIER_WATCHING):
        return tier, True if pinned is None else pinned
    return tier, False if pinned is None else pinned


def replacement_for_core(prompts: list[Any], value_by_id: dict[str, float | None] | None = None, exclude: str | None = None) -> Any | None:
    """Which core question makes room when the owner puts one in a full core:
    None when core has a free spot; otherwise the lowest-value UNPINNED,
    non-owner-written core question (unmeasured counts as 0; ties: the
    lower research rank goes). Raises LookupError when core is full of the
    owner's own picks. Pure."""
    values = value_by_id or {}
    core = [prompt for prompt in prompts if getattr(prompt, "tier", None) == TIER_CORE and prompt.id != exclude and getattr(prompt, "retired_at", None) is None]
    if len(core) < CORE_SLOTS:
        return None
    movable = [prompt for prompt in core if not prompt.pinned and getattr(prompt, "source", None) != SOURCE_USER]
    if not movable:
        raise LookupError("core is full of pinned questions")
    return min(movable, key=lambda prompt: (values.get(prompt.id) or 0.0, -(prompt.created_at or datetime.min).timestamp(), prompt.id))


##############################
# LOADERS
##############################


async def brand_checks(brand_id: str, prompt_ids: list[str], now: datetime, weeks: int, engines: list[str], mention_weeks: int | None = None, mention_prompt_ids: Iterable[str] = ()) -> dict[str, list[CheckSummary | None]]:
    """Per question, its weekly checks (index 0 = this week; None = not asked
    that week), dead-zone states chained over the `weeks` read. Competitor
    mentions (for the competitor signal / value / dead) are loaded for the
    answers in the newest `mention_weeks` weeks (default: all) plus every week
    of `mention_prompt_ids` — the multi-week read stays affordable."""
    if not prompt_ids:
        return {}
    rows = await answer_stats.analyzed_rows(brand_id, since=now - answer_stats.WEEK * weeks, until=now, prompt_ids=prompt_ids)
    buckets_by_prompt: dict[str, list[list[Any]]] = {prompt_id: [[] for _ in range(weeks)] for prompt_id in prompt_ids}
    for index, bucket in enumerate(answer_stats.weekly_buckets([row for row in rows if row.engine in engines], now, weeks)):
        for row in bucket:
            buckets_by_prompt.setdefault(row.prompt_id, [[] for _ in range(weeks)])[index].append(row)

    always = set(mention_prompt_ids)
    answers_by_prompt_week: dict[tuple[str, int], list[Any]] = {}
    wanted: list[Any] = []
    for prompt_id, buckets in buckets_by_prompt.items():
        for index, bucket in enumerate(buckets):
            answers = [answer for engine in engines for answer in answer_stats.latest_per_market(row for row in bucket if row.engine == engine)]
            answers_by_prompt_week[(prompt_id, index)] = answers
            if mention_weeks is None or index < mention_weeks or prompt_id in always:
                wanted += answers
    mentions = await answer_stats.mentions_by_result(brand_id, wanted) if wanted else {}

    checks: dict[str, list[CheckSummary | None]] = {}
    for prompt_id, buckets in buckets_by_prompt.items():
        history = answer_stats.prompt_history(buckets, engines)
        checks[prompt_id] = [check_summary(history[index], answers_by_prompt_week[(prompt_id, index)], mentions) if answer_stats.asked_in(history[index]) else None for index in range(weeks)]
    return checks


async def core_values(brand_id: str, prompts: list[Any], now: datetime, engines: list[str]) -> dict[str, float | None]:
    """The value of each core question at its latest check in the last 2
    weeks — the owner endpoints' "who makes room" read (no Search Console call)."""
    core_ids = [prompt.id for prompt in prompts if getattr(prompt, "tier", None) == TIER_CORE]
    checks = await brand_checks(brand_id, core_ids, now, CONSECUTIVE_CHECKS, engines)
    return {prompt_id: question_value(next((check for check in checks.get(prompt_id, []) if check is not None), None)) for prompt_id in core_ids}


##############################
# WRITERS
##############################


async def record_move(brand_id: str, prompt_id: str, from_tier: str, to_tier: str, reason_code: str, automatic: bool = True, pinned: bool | None = None) -> None:
    """Move one question and record it (the recap + swap budget read the record)."""
    if await Prompt.set_tier(brand_id, prompt_id, to_tier, REASONS[reason_code], pinned=pinned):
        await QuestionMove.create(brand_id=brand_id, prompt_id=prompt_id, from_tier=from_tier, to_tier=to_tier, reason_code=reason_code, reason=REASONS[reason_code], automatic=automatic)


async def assign_initial_tiers(brand_id: str, prompts: list[Any]) -> dict[str, str]:
    """Write initial_tiers (no move rows: a starting point, not a move).
    Returns what was assigned ({} = nothing to do)."""
    tiers = initial_tiers(prompts)
    for prompt_id, tier in tiers.items():
        await Prompt.set_tier(brand_id, prompt_id, tier, REASONS[REASON_INITIAL_CORE if tier == TIER_CORE else REASON_INITIAL_WATCHING])
    return tiers
