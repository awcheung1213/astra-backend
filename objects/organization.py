import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, String, func
from sqlmodel import Field, SQLModel, delete

from exceptions.exceptions import OrganizationNotFound
from objects.base_object import BaseObject
from utils import clock
from utils.database import Database, ReplicaDatabase


class Organization(SQLModel, BaseObject, table=True):
    __tablename__ = "organizations"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    name: str
    plan: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def get(id: str) -> "Organization":
        organization = await ReplicaDatabase.session.get(Organization, id)
        if not organization:
            raise OrganizationNotFound()
        return organization

    @staticmethod
    async def create(name: str, plan: str | None = None) -> "Organization":
        organization = Organization(name=name, plan=plan)
        session = Database.session
        session.add(organization)
        await session.flush()
        await session.refresh(organization)
        return organization

    @staticmethod
    async def update(id: str, name: str | None = None, plan: str | None = None) -> "Organization":
        organization = await Organization.get(id)
        if name is not None:
            organization.name = name
        if plan is not None:
            organization.plan = plan
        session = Database.session
        session.add(organization)
        await session.flush()
        await session.refresh(organization)
        return organization

    @staticmethod
    async def delete(id: str) -> None:
        await Database.session.execute(delete(Organization).where(Organization.id == id))  # type: ignore
