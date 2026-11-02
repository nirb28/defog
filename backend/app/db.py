"""Database engine and sessions. Production uses Supabase Postgres; tests use a local Postgres."""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings


def normalize_database_url(url: str) -> str:
    """Accept the connection string exactly as Supabase shows it.

    - postgres:// and postgresql:// become postgresql+psycopg:// (the driver we ship).
    - Supabase hosts get sslmode=require unless the URL already sets an sslmode.
    """
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    query = dict(parse_qsl(parts.query))
    if (host.endswith(".supabase.co") or host.endswith(".supabase.com")) and "sslmode" not in query:
        query["sslmode"] = "require"
    return urlunsplit(parts._replace(query=urlencode(query)))


def uses_transaction_pooler(url: str) -> bool:
    """Supabase's transaction pooler (port 6543) can't run prepared statements."""
    return urlsplit(url).port == 6543


def build_engine(url: str, pool_size: int = 5) -> Engine:
    url = normalize_database_url(url)
    connect_args: dict = {}
    if uses_transaction_pooler(url):
        connect_args["prepare_threshold"] = None
    return create_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,  # survive Supabase/pooler closing idle connections
        pool_recycle=300,
        connect_args=connect_args,
    )


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return build_engine(settings.database_url, settings.database_pool_size)


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, rolled back if the request fails."""
    db = _session_factory()()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """For code outside requests (background sync, streaming chat)."""
    db = _session_factory()()
    try:
        yield db
    finally:
        db.close()
