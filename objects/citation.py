import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlmodel import Field, SQLModel, delete

from objects.base_object import BaseObject
from utils import clock
from utils.database import Database
from utils.urls import domain_from_url


class Citation(SQLModel, BaseObject, table=True):
    """Own table so 'top cited domains' is a GROUP BY, not JSONB surgery."""

    __tablename__ = "citations"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    prompt_result_id: str = Field(sa_column=Column(String, ForeignKey("prompt_results.id", ondelete="CASCADE"), nullable=False, index=True))
    url: str = Field(sa_column=Column(Text, nullable=False))
    domain: str = Field(sa_column=Column(String, nullable=False, index=True))
    title: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def create(
        brand_id: str,
        prompt_id: str,
        prompt_result_id: str,
        cited_text: str,
        title: str,
        type: str,
        url: str,
        domain: str | None = None,
    ) -> "Citation":
        citation = Citation(
            brand_id=brand_id,
            prompt_id=prompt_id,
            prompt_result_id=prompt_result_id,
            cited_text=cited_text,
            title=title,
            type=type,
            url=url,
            domain=domain or domain_from_url(url),
        )
        session = Database.session
        session.add(citation)
        await session.flush()
        await session.refresh(citation)
        return citation

    @staticmethod
    async def update(id: str, cited_text: str, title: str, type: str, url: str) -> "Citation":
        citation = await Citation.get(id)
        citation.cited_text = cited_text
        citation.title = title
        citation.type = type
        citation.url = url
        citation.domain = domain_from_url(url)
        session = Database.session
        session.add(citation)
        await session.flush()
        await session.refresh(citation)
        return citation

    @staticmethod
    async def delete(id: str) -> None:
        await Database.session.execute(delete(Citation).where(Citation.id == id))  # type: ignore
