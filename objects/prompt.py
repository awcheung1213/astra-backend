import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, String, Text, func, select, update
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils import clock
from utils.database import Database, ReplicaDatabase

# question levels (utils/question_tiers.py owns the rules; re-exported there)
TIER_CORE = "core"  # weekly, full panel — the AI answers tiles
TIER_WATCHING = "watching"  # the long tail, checked in a monthly rotation
TIER_SUGGESTED = "suggested"  # discovered, not checked
TIERS = (TIER_CORE, TIER_WATCHING, TIER_SUGGESTED)

SOURCE_GENERATED = "generated"
SOURCE_USER = "user"  # owner-written: never moved automatically
SOURCE_GSC = "gsc"


class Prompt(SQLModel, BaseObject, table=True):
    """A location-agnostic template; the visibility runner expands
    prompts x markets x engines, injecting each market's city.

    `tier` is the question's level (core | watching | suggested); collection
    gets its list ONLY from utils/question_tiers.questions_to_collect.
    `is_active` is kept as a mirror of `tier == core` (the tiles; older FE
    builds filter on it) — every tier write goes through set_tier, which
    keeps the two in step."""

    __tablename__ = "prompts"
    __table_args__ = (Index("ix_prompts_brand_id_tier", "brand_id", "tier"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    text: str = Field(sa_column=Column(Text, nullable=False))
    category: str
    # a 2-4 word plain label for tiles ("Emergency plumber"); NULL = not yet
    # labeled (pre-topic rows until backfill_question_topics runs) — the FE
    # falls back to the question's first words
    topic: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    source: str = SOURCE_GENERATED  # generated | user | gsc
    is_active: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    tier: str = Field(default=TIER_SUGGESTED, sa_column=Column(String, server_default=TIER_SUGGESTED, nullable=False))
    # owner-set level: the weekly sorter never moves a pinned question
    pinned: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    # plain words for the latest move ("Competitors get named here and you don't yet")
    tier_reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    tier_changed_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    # when collection last picked it (core: every run; watching: its rotation turn)
    last_checked_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    # a move signal seen on the latest check, waiting for its confirming second
    # check (the rotation re-checks these first); NULL = nothing pending
    pending_signal: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    # retiring stamps a date, history stays
    retired_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def create(
        brand_id: str,
        text: str,
        category: str,
        source: str = SOURCE_GENERATED,
        created_at: datetime | None = None,
        topic: str | None = None,
        tier: str = TIER_SUGGESTED,
        pinned: bool = False,
        tier_reason: str | None = None,
    ) -> "Prompt":
        """created_at: research stamps its reply order (most-asked first) so
        the rank survives — one transaction's now() would tie every row."""
        prompt = Prompt(brand_id=brand_id, text=text, category=category, source=source, topic=topic, tier=tier, is_active=tier == TIER_CORE, pinned=pinned, tier_reason=tier_reason)
        if tier != TIER_SUGGESTED:
            prompt.tier_changed_at = clock.now()
        if created_at is not None:
            prompt.created_at = created_at
        session = Database.session
        session.add(prompt)
        await session.flush()
        await session.refresh(prompt)
        return prompt

    @staticmethod
    async def set_tier(brand_id: str, prompt_id: str, tier: str, reason: str | None, pinned: bool | None = None) -> int:
        """Move one question to a level (scoped to the brand; retired rows are
        never moved). Keeps is_active == (tier == core). pinned None = leave it.
        Returns the rowcount (0 = not this brand's / retired)."""
        values: dict = {"tier": tier, "is_active": tier == TIER_CORE, "tier_reason": reason, "tier_changed_at": clock.now(), "pending_signal": None}
        if pinned is not None:
            values["pinned"] = pinned
        result = await Database.session.execute(update(Prompt).where(Prompt.brand_id == brand_id).where(Prompt.id == prompt_id).where(Prompt.retired_at.is_(None)).values(**values))  # type: ignore
        await Database.session.flush()
        return result.rowcount

    @staticmethod
    async def set_pinned(brand_id: str, prompt_id: str, pinned: bool) -> int:
        result = await Database.session.execute(update(Prompt).where(Prompt.brand_id == brand_id).where(Prompt.id == prompt_id).where(Prompt.retired_at.is_(None)).values(pinned=pinned))  # type: ignore
        await Database.session.flush()
        return result.rowcount

    @staticmethod
    async def mark_checked(brand_id: str, ids: list[str], at: datetime) -> None:
        """Stamp last_checked_at — collection picked these for this run."""
        if not ids:
            return
        await Database.session.execute(update(Prompt).where(Prompt.brand_id == brand_id).where(Prompt.id.in_(ids)).values(last_checked_at=at))  # type: ignore
        await Database.session.flush()

    @staticmethod
    async def set_pending_signals(brand_id: str, pending_by_id: dict[str, str | None]) -> None:
        """The sorter's per-question pending signal (None clears it)."""
        for prompt_id, signal in pending_by_id.items():
            await Database.session.execute(update(Prompt).where(Prompt.brand_id == brand_id).where(Prompt.id == prompt_id).values(pending_signal=signal))  # type: ignore
        await Database.session.flush()

    @staticmethod
    async def retire(brand_id: str, prompt_id: str) -> int:
        """Stop a question for good (an owner's edit replaces it): stamped,
        out of every level, its history kept."""
        result = await Database.session.execute(update(Prompt).where(Prompt.brand_id == brand_id).where(Prompt.id == prompt_id).where(Prompt.retired_at.is_(None)).values(retired_at=clock.now(), is_active=False, pending_signal=None))  # type: ignore
        await Database.session.flush()
        return result.rowcount

    @staticmethod
    async def by_brand(brand_id: str) -> list["Prompt"]:
        stmt = select(Prompt).where(Prompt.retired_at.is_(None)).where(Prompt.brand_id == brand_id)
        rows = await ReplicaDatabase.session.execute(stmt)
        return list(rows.scalars().all())

    @staticmethod
    async def get_for_brand(brand_id: str, prompt_id: str) -> "Prompt | None":
        """One prompt, only if it is this brand's (retired included: its
        history stays readable). None = not found / another brand's."""
        rows = await ReplicaDatabase.session.execute(select(Prompt).where(Prompt.id == prompt_id).where(Prompt.brand_id == brand_id))  # type: ignore
        return rows.scalars().first()

    @staticmethod
    async def get_live(brand_id: str, prompt_id: str) -> "Prompt | None":
        """One non-retired prompt of this brand, read on the PRIMARY session
        (so a row written earlier in this request is visible)."""
        statement = select(Prompt).where(Prompt.id == prompt_id).where(Prompt.brand_id == brand_id).where(Prompt.retired_at.is_(None)).execution_options(populate_existing=True)  # type: ignore
        rows = await Database.session.execute(statement)
        return rows.scalars().first()

    @staticmethod
    async def set_topic(prompt_id: str, topic: str) -> bool:
        """Fill a missing topic (the backfill). Only writes while topic IS NULL,
        so a label that landed meanwhile is never overwritten."""
        result = await Database.session.execute(update(Prompt).where(Prompt.id == prompt_id).where(Prompt.topic.is_(None)).values(topic=topic))  # type: ignore
        await Database.session.flush()
        return bool(result.rowcount)
