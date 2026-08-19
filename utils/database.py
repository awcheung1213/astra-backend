from contextlib import asynccontextmanager

from sqlalchemy import engine, pool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import settings
from context import database_session


class MissingSessionException(Exception):
    def __init__(self) -> None:
        super().__init__("missing session")


class Database:
    __instance = None
    __engine: AsyncEngine
    static_session: AsyncSession | None = None

    def __new__(cls) -> "Database":
        # create a singleton instance of the database engine
        if cls.__instance is None:
            cls.__instance = object.__new__(cls)
            cls.__engine = cls.__create_engine()
        return cls.__instance

    @classmethod  # type: ignore
    @property
    def session(cls) -> AsyncSession:
        return cls().get_session()

    @asynccontextmanager
    async def connection(self):
        try:
            yield self.start_session()
        finally:
            await self.close_session()

    def start_session(self) -> AsyncSession:
        # start a new database session
        try:
            database_session.set(self.create_session())  # type: ignore
        except:  # noqa
            self.static_session = self.create_session()
        return self.get_session()

    def get_session(self) -> AsyncSession:
        # get the current database session
        if self.static_session is not None:
            return self.static_session
        else:
            try:
                return database_session.get()  # type: ignore
            except:  # noqa
                raise MissingSessionException()

    async def close_session(self) -> None:
        # close the current database session
        session = self.get_session()
        self.static_session = None
        await session.close()

    def create_session(self) -> AsyncSession:
        # create a new database session for interacting with the database
        session = sessionmaker(self.__engine, class_=AsyncSession, expire_on_commit=False)()  # type: ignore

        # rename session.begin to session.transaction
        session.transaction = session.begin

        return session

    @classmethod
    def __create_engine(cls) -> AsyncEngine:
        # create a new database engine to be used when creating sessions
        connection_url = engine.URL.create(
            "postgresql+asyncpg",
            settings.DATABASE_USERNAME,
            settings.DATABASE_PASSWORD,
            settings.DATABASE_HOST,
            settings.DATABASE_PORT,
            settings.DATABASE_NAME,
        )
        return create_async_engine(
            connection_url,
            isolation_level="READ_COMMITTED",
            poolclass=pool.NullPool,
        )


class ReplicaDatabase(Database):
    __instance = None

    @classmethod
    def __create_engine(cls) -> AsyncEngine:
        # create a new database engine to be used when creating sessions
        connection_url = engine.URL.create(
            "postgresql+asyncpg",
            settings.DATABASE_USERNAME,
            settings.DATABASE_PASSWORD,
            settings.REPLICA_DATABASE_HOST,
            settings.DATABASE_PORT,
            settings.DATABASE_NAME,
        )
        return create_async_engine(
            connection_url,
            isolation_level="READ_COMMITTED",
            poolclass=pool.NullPool,
        )
