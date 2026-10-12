import re
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, func, select
from sqlmodel import Field, SQLModel

from exceptions.exceptions import CompetitorNotFound
from objects.base_object import BaseObject
from utils import clock
from utils.database import Database, ReplicaDatabase

# rule 5: machine rows carry their source; the system never revises a human's
SOURCE_AI_BOOTSTRAP = "ai_bootstrap"  # research_competitors seeding
SOURCE_USER = "user"  # client add (competitors API / onboarding step)
SOURCE_DISCOVERED = "discovered"  # names found in collected answers


class Competitor(SQLModel, BaseObject, table=True):
    """Three states: active = charted; inactive = still matched, history
    accrues; excluded = never matched, never resurrected."""

    __tablename__ = "competitors"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    name: str
    normalized_name: str
    source: str = SOURCE_AI_BOOTSTRAP  # ai_bootstrap | user | discovered
    is_active: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    is_excluded: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def _get(competitor_id: str, brand_id: str | None = None) -> "Competitor":
        competitor = await ReplicaDatabase.session.get(Competitor, competitor_id)
        if not competitor or (brand_id and competitor.brand_id != brand_id):
            raise CompetitorNotFound()
        return competitor

    @staticmethod
    def is_own_brand(brand_name: str, name: str) -> bool:
        brand_normalized = Competitor.normalize_name(brand_name)
        name_normalized = Competitor.normalize_name(name)
        return brand_normalized in name_normalized or name_normalized in brand_normalized

    @staticmethod
    def normalize_name(name: str) -> str:
        # Split camelCase/PascalCase before lowercasing (PepsiCo → Pepsi Co)
        n = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
        n = n.lower()
        # Hyphens/underscores become word boundaries
        n = re.sub(r"[-_]", " ", n)
        # Strip everything except letters, digits, and spaces
        n = re.sub(r"[^a-z0-9 ]", "", n)
        words = n.split()
        # Drop leading "the"
        if words and words[0] == "the":
            words = words[1:]
        # Drop trailing common business entity suffixes
        suffixes = {"co", "corp", "inc", "ltd", "llc", "company", "corporation", "incorporated", "limited"}
        while words and words[-1] in suffixes:
            words.pop()
        return "".join(words)

    @staticmethod
    def upsert_revises(existing_source: str, source: str) -> bool:
        """Whether an upsert from `source` may re-spell a row whose source is
        `existing_source`: the user always may; the system only its own rows."""
        return source == SOURCE_USER or existing_source != SOURCE_USER

    @staticmethod
    async def get_competitors_by_brand_id(brand_id: str) -> list["Competitor"]:
        """The brand's non-excluded competitor rows — the matcher's input."""
        rows = await ReplicaDatabase.session.execute(select(Competitor).where(Competitor.brand_id == brand_id, Competitor.is_excluded == False))  # type: ignore  # noqa: E712
        return list(rows.scalars().all())

    @staticmethod
    async def excluded_names(brand_id: str) -> set[str]:
        """Normalized names this brand has excluded - the matcher skips them."""
        rows = await ReplicaDatabase.session.execute(select(Competitor.normalized_name).where(Competitor.brand_id == brand_id, Competitor.is_excluded == True))  # type: ignore  # noqa: E712
        return set(rows.scalars().all())

    @staticmethod
    async def create(
        brand_id: str,
        name: str,
        normalized_name: str,
        source: str,
        is_active: bool | None = None,
        is_excluded: bool | None = None,
    ) -> "Competitor":
        competitor = Competitor(brand_id=brand_id, name=name, normalized_name=normalized_name, source=source)
        if is_active is not None:
            competitor.is_active = is_active
        if is_excluded is not None:
            competitor.is_excluded = is_excluded
        session = Database.session
        session.add(competitor)
        await session.flush()
        await session.refresh(competitor)
        return competitor

    @staticmethod
    async def update(
        competitor_id: str,
        brand_id: str | None = None,
        name: str | None = None,
        normalized_name: str | None = None,
        source: str | None = None,
        is_active: bool | None = None,
        is_excluded: bool | None = None,
    ) -> "Competitor":
        competitor = await Competitor._get(competitor_id, brand_id=brand_id)
        if name is not None:
            competitor.name = name
        if normalized_name is not None:
            competitor.normalized_name = normalized_name
        if source is not None:
            competitor.source = source
        if is_active is not None:
            competitor.is_active = is_active
        if is_excluded is not None:
            competitor.is_excluded = is_excluded
        session = Database.session
        session.add(competitor)
        await session.flush()
        await session.refresh(competitor)
        return competitor

    @staticmethod
    async def upsert(
        brand_id: str,
        name: str,
        source: str,
        is_active: bool | None = None,
        is_excluded: bool | None = None,
    ) -> "Competitor":
        """Create, or revise the row with the same normalized name. A user
        upsert claims the row (their spelling, source=user); a machine upsert
        re-spells only machine rows and never changes an existing row's
        source, so a human's row is never revised by the system (rule 5)."""
        normalized_name = Competitor.normalize_name(name)

        rows = await ReplicaDatabase.session.execute(
            select(Competitor).where(
                Competitor.brand_id == brand_id,
                Competitor.normalized_name == normalized_name,
            )
        )
        competitor = rows.scalars().first()
        if competitor:
            revises = Competitor.upsert_revises(competitor.source, source)
            return await Competitor.update(
                competitor_id=competitor.id,
                name=name if revises else None,
                source=source if source == SOURCE_USER else None,
                is_active=is_active,
                is_excluded=is_excluded,
            )
        return await Competitor.create(
            brand_id=brand_id,
            name=name,
            normalized_name=normalized_name,
            source=source,
            is_active=is_active,
            is_excluded=is_excluded,
        )
