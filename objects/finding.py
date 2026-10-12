import uuid
from datetime import date, datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils import clock
from utils.database import Database, ReplicaDatabase

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_SERIOUS = "serious"

STATUS_OPEN = "open"
STATUS_FIXED = "fixed"
STATUS_DISMISSED = "dismissed"
STATUS_DUPLICATE = "duplicate"  # a twin closed by the audit (DEC-020): nothing was fixed, so resolved counts (STATUS_FIXED) skip it


class Finding(SQLModel, BaseObject, table=True):
    """Audit rule-engine output. Powers the Site health page (open issues,
    resolved-this-month, the 94/100 score derives from these) and feeds the
    fix generators — a finding's kind maps to a fix task; odd cases escalate
    to the Technical agent. Lifecycle: open -> fixed | dismissed | duplicate."""

    __tablename__ = "findings"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    site_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("sites.id"), nullable=True, index=True))
    kind: str  # missing_schema | slow_page | broken_link | blocked_ai_crawler | ...
    severity: str = Field(default=SEVERITY_WARN, sa_column=Column(String, server_default=SEVERITY_WARN, nullable=False))
    summary: str = Field(sa_column=Column(Text, nullable=False))  # client-readable one-liner
    detail: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))  # machine payload for fix generators
    status: str = Field(default=STATUS_OPEN, sa_column=Column(String, server_default=STATUS_OPEN, nullable=False))
    resolved_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def create(
        brand_id: str,
        kind: str,
        summary: str,
        site_id: str | None = None,
        severity: str = SEVERITY_WARN,
        detail: dict | None = None,
    ) -> "Finding":
        finding = Finding(brand_id=brand_id, site_id=site_id, kind=kind, severity=severity, summary=summary, detail=detail or {})
        session = Database.session
        session.add(finding)
        await session.flush()
        await session.refresh(finding)
        return finding

    @staticmethod
    async def open_for_brand(brand_id: str) -> list["Finding"]:
        rows = await ReplicaDatabase.session.execute(select(Finding).where(Finding.brand_id == brand_id).where(Finding.status == STATUS_OPEN))
        return list(rows.scalars().all())

    @staticmethod
    async def open_as_of(brand_id: str, at: datetime) -> list["Finding"]:
        """Findings that were open at `at` — found before it and not fixed by then
        (a period report counts its own month, not findings opened after it)."""
        still_open = Finding.status == STATUS_OPEN
        fixed_later = (Finding.status == STATUS_FIXED) & (Finding.resolved_at >= at)  # type: ignore
        rows = await ReplicaDatabase.session.execute(select(Finding).where(Finding.brand_id == brand_id).where(Finding.created_at < at).where(still_open | fixed_later))  # type: ignore
        return list(rows.scalars().all())

    @staticmethod
    async def resolved_since(brand_id: str, since: date, until: date | None = None) -> list["Finding"]:
        """Fixed in [since, until); open-ended when until is None."""
        statement = select(Finding).where(Finding.brand_id == brand_id).where(Finding.status == STATUS_FIXED).where(Finding.resolved_at >= since)  # type: ignore
        if until is not None:
            statement = statement.where(Finding.resolved_at < until)  # type: ignore
        rows = await ReplicaDatabase.session.execute(statement)
        return list(rows.scalars().all())

    @staticmethod
    async def resolve(id: str, status: str = STATUS_FIXED) -> None:
        """open -> fixed; the audit auto-resolves findings it no longer detects
        (and closes twins as STATUS_DUPLICATE)."""
        finding = await Database.session.get(Finding, id)
        if finding is None:
            return
        finding.status = status
        finding.resolved_at = clock.now()
        Database.session.add(finding)
        await Database.session.flush()
