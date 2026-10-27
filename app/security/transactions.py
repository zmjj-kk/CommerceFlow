from contextlib import asynccontextmanager
from sqlalchemy import text
import app.db.base as db


@asynccontextmanager
async def transaction():
    """MySQL row locks in services; SQLite serializes writes before the first read."""
    async with db.async_session() as s, s.begin():
        if s.bind.dialect.name == "sqlite":
            await s.execute(text("BEGIN IMMEDIATE"))
        yield s
