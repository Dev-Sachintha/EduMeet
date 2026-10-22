"""SQLAlchemy engine, session factory and declarative base.

Default: SQLite (a single file, nothing to install). PostgreSQL works too:
set DATABASE_URL=postgresql+psycopg2://user:password@localhost:5432/edumeet in .env
"""
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from backend.config import BASE_DIR, settings


def _build_engine():
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite":
        # Relative sqlite paths are placed inside the project folder (not the current directory).
        if url.database and url.database != ":memory:" and not Path(url.database).is_absolute():
            url = url.set(database=str(BASE_DIR / url.database))
        # FastAPI uses several threads; SQLite must allow that.
        eng = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # fewer "database is locked" errors
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        return eng
    return create_engine(url, pool_pre_ping=True)


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def ensure_columns(db_engine) -> None:
    """Tiny auto-migration: add columns that newer code expects but an older database file lacks.

    (create_all() creates missing TABLES but never adds columns.) Use Alembic for real migrations.
    """
    inspector = inspect(db_engine)
    with db_engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(db_engine.dialect)}"
                if col.server_default is not None:
                    arg = col.server_default.arg
                    default = str(arg.compile(dialect=db_engine.dialect)) if hasattr(arg, "compile") else str(arg)
                    ddl += f" DEFAULT {default}"
                    if not col.nullable:
                        ddl += " NOT NULL"
                conn.exec_driver_sql(ddl)
