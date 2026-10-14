import uuid
from datetime import datetime, timedelta
from itertools import batched

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, UniqueConstraint, case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils import clock
from utils.database import Database, ReplicaDatabase

STATUS_UNCHECKED = "unchecked"
STATUS_HEALTHY = "healthy"
STATUS_NEEDS_FIX = "needs_fix"
STATUS_SLOW = "slow"
STATUS_BROKEN = "broken"
PAGE_STATUSES = (STATUS_UNCHECKED, STATUS_HEALTHY, STATUS_NEEDS_FIX, STATUS_SLOW, STATUS_BROKEN)

# a page stays CURRENT while something keeps vouching for it each audit (its
# sitemap listing, a link in link-crawl mode, an open problem on it). One that
# stops being seen is kept, never deleted, but drops out of the counts and the
# rotation after this many days (~4 weekly audits — rides out a flaky sitemap)
AGE_OUT_DAYS = 30
UPSERT_CHUNK = 1000  # rows per INSERT ... ON CONFLICT (asyncpg's 32k bind-param ceiling)


def current_since() -> datetime:
    return clock.now() - timedelta(days=AGE_OUT_DAYS)


class SitePage(SQLModel, BaseObject, table=True):
    """One row per page of a brand's site — the TRUE page list (from the
    sitemap, else the link crawl) the "Your site" screen counts, and the
    audit's rotation state: each run checks at most MAX_PAGES of them
    (worker/tasks/site_audit.py), oldest-checked first, so every page gets
    its turn. url is normalized (site_audit.normalize_page_url) — the same
    spelling findings key on. Machine rows: the audit revises them freely."""

    __tablename__ = "site_pages"
    __table_args__ = (UniqueConstraint("brand_id", "url"), Index("ix_site_pages_brand_id_last_checked_at", "brand_id", "last_checked_at"))

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False))
    url: str = Field(sa_column=Column(String, nullable=False))
    section: str = Field(sa_column=Column(String, nullable=False))  # "Main pages" | "Blog posts" | "Service areas" | ...
    status: str = Field(default=STATUS_UNCHECKED, sa_column=Column(String, server_default=STATUS_UNCHECKED, nullable=False))  # unchecked | healthy | needs_fix | slow | broken
    first_seen_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))
    last_seen_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))
    last_checked_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))

    @staticmethod
    async def for_brand(brand_id: str) -> list["SitePage"]:
        """Every page row (current or aged out) — the audit's rotation input."""
        rows = await ReplicaDatabase.session.execute(select(SitePage).where(SitePage.brand_id == brand_id))  # type: ignore
        return list(rows.scalars().all())

    @staticmethod
    async def upsert_seen(brand_id: str, sections_by_url: dict[str, str], seen_at: datetime) -> None:
        """Pages this audit saw: new ones land unchecked; known ones get their
        section refreshed and last_seen_at bumped (status/checks untouched)."""
        rows = [{"id": str(uuid.uuid4()), "brand_id": brand_id, "url": url, "section": section, "first_seen_at": seen_at, "last_seen_at": seen_at} for url, section in sections_by_url.items()]
        for chunk in batched(rows, UPSERT_CHUNK):
            statement = pg_insert(SitePage).values(list(chunk))
            statement = statement.on_conflict_do_update(index_elements=["brand_id", "url"], set_={"section": statement.excluded.section, "last_seen_at": statement.excluded.last_seen_at})
            await Database.session.execute(statement)

    @staticmethod
    async def record_checks(brand_id: str, checks: list[tuple[str, str, str]], checked_at: datetime) -> None:
        """(url, section, status) for each page this run actually checked:
        status + last_checked_at land; a page not yet a row is inserted."""
        rows = [{"id": str(uuid.uuid4()), "brand_id": brand_id, "url": url, "section": section, "status": status, "first_seen_at": checked_at, "last_seen_at": checked_at, "last_checked_at": checked_at} for url, section, status in checks]
        for chunk in batched(rows, UPSERT_CHUNK):
            statement = pg_insert(SitePage).values(list(chunk))
            statement = statement.on_conflict_do_update(index_elements=["brand_id", "url"], set_={"status": statement.excluded.status, "last_checked_at": statement.excluded.last_checked_at})
            await Database.session.execute(statement)

    @staticmethod
    async def status_counts(brand_id: str) -> list[tuple[str, str, int, datetime | None]]:
        """(section, status, pages, newest last_checked_at) over CURRENT pages —
        the worklog's site_pages rollup, aggregated in SQL (rule 4)."""
        rows = await ReplicaDatabase.session.execute(select(SitePage.section, SitePage.status, func.count(), func.max(SitePage.last_checked_at)).where(SitePage.brand_id == brand_id).where(SitePage.last_seen_at >= current_since()).group_by(SitePage.section, SitePage.status))  # type: ignore
        return [(section, status, int(count), last_checked_at) for section, status, count, last_checked_at in rows]

    @staticmethod
    async def search(brand_id: str, q: str | None = None, section: str | None = None, limit: int = 20) -> list["SitePage"]:
        """Current pages whose url contains q (case-insensitive), optionally in
        one section; problems first, then by url."""
        statement = select(SitePage).where(SitePage.brand_id == brand_id).where(SitePage.last_seen_at >= current_since())  # type: ignore
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            statement = statement.where(SitePage.url.ilike(f"%{escaped}%", escape="\\"))  # type: ignore
        if section:
            statement = statement.where(SitePage.section == section)  # type: ignore
        problems_first = case({STATUS_BROKEN: 0, STATUS_NEEDS_FIX: 1, STATUS_SLOW: 2, STATUS_HEALTHY: 3}, value=SitePage.status, else_=4)
        rows = await ReplicaDatabase.session.execute(statement.order_by(problems_first, SitePage.url).limit(limit))  # type: ignore
        return list(rows.scalars().all())
