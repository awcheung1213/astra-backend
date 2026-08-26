import re
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, func, select
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils.database import Database, ReplicaDatabase
from exceptions.exceptions import CompetitorNotFound


class Competitor(SQLModel, BaseObject, table=True):
    """Three states: active = charted; inactive = still matched, history
    accrues; excluded = never matched, never resurrected."""

    __tablename__ = "competitors"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    name: str
    normalized_name: str
    source: str = "ai_bootstrap"  # ai_bootstrap | user | discovered
    is_active: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    is_excluded: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

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
    async def create(
        brand_id: str,
        name: str,
        normalized_name: str,
        is_active: bool | None = None,
        is_excluded: bool | None = None,
    ) -> "Competitor":
        competitor = Competitor(brand_id=brand_id, name=name, normalized_name=normalized_name)
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
        is_active: bool | None = None,
        is_excluded: bool | None = None,
    ) -> "Competitor":
        competitor = await Competitor._get(competitor_id, brand_id=brand_id)
        if name is not None:
            competitor.name = name
        if normalized_name is not None:
            competitor.normalized_name = normalized_name
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
        is_active: bool | None = None,
        is_excluded: bool | None = None,
    ) -> "Competitor":
        normalized_name = Competitor.normalize_name(name)

        rows = await ReplicaDatabase.session.execute(
            select(Competitor).where(
                Competitor.brand_id == brand_id,
                Competitor.normalized_name == normalized_name,
            )
        )
        competitor = rows.scalars().first()
        if competitor:
            return await Competitor.update(
                competitor_id=competitor.id,
                name=name,
                normalized_name=normalized_name,
                is_active=is_active,
                is_excluded=is_excluded,
            )
        return await Competitor.create(
            brand_id=brand_id,
            name=name,
            normalized_name=normalized_name,
            is_active=is_active,
            is_excluded=is_excluded,
        )
