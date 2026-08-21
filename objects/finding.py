import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject

SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_SERIOUS = "serious"

STATUS_OPEN = "open"
STATUS_FIXED = "fixed"
STATUS_DISMISSED = "dismissed"


class Finding(SQLModel, BaseObject, table=True):
    """Audit rule-engine output. Powers the Site health page (open issues,
    resolved-this-month, the 94/100 score derives from these) and feeds the
    fix generators — a finding's kind maps to a fix task; odd cases escalate
    to the Technical agent. Lifecycle: open -> fixed | dismissed."""

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
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))
