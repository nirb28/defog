"""Defog API entry point. Run locally with: uvicorn app.main:app --reload"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete, or_

from . import account
from .ai import get_llm  # noqa: F401 - re-exported for tests and callers
from .auth.routes import router as auth_router
from .auth.sessions import CSRF_HEADER
from .chat.routes import router as chat_router
from .config import Settings, get_settings
from .connections.routes import router as connections_router
from .connections.sync import due_connection_ids, sync_connection
from .db import session_scope
from .models import AuthToken, LoginFailure, Session, utcnow
from .schemas import HealthResponse

log = logging.getLogger("defog")
settings = get_settings()

SYNC_LOOP_SECONDS = 600  # look for connections due a sync every 10 minutes


def purge_expired() -> None:
    """Delete sessions, links and login-failure records nobody can use any more."""
    now = utcnow()
    with session_scope() as db:
        db.execute(
            delete(Session).where(
                Session.expires_at < now,
                or_(Session.refresh_expires_at.is_(None), Session.refresh_expires_at < now),
            )
        )
        db.execute(delete(AuthToken).where(AuthToken.expires_at < now - timedelta(days=1)))
        db.execute(delete(LoginFailure).where(LoginFailure.created_at < now - timedelta(days=1)))
        db.commit()


async def sync_loop() -> None:
    """Keep every connection fresh (every SYNC_INTERVAL_MINUTES) and retry failed syncs."""
    while True:
        try:
            for connection_id in await asyncio.to_thread(due_connection_ids):
                await asyncio.to_thread(sync_connection, connection_id)
            await asyncio.to_thread(purge_expired)
        except Exception:  # noqa: BLE001 - keep the loop alive; the next pass retries
            log.exception("Background sync pass failed")
        await asyncio.sleep(SYNC_LOOP_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    task = asyncio.create_task(sync_loop()) if settings.background_sync_enabled else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Defog API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # The web app sends its session cookie to the API (same site, different subdomain).
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization", CSRF_HEADER],
)


@app.middleware("http")
async def security_headers(request, call_next):  # noqa: ANN001, ANN201
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


app.include_router(auth_router)
app.include_router(account.router)
app.include_router(connections_router)
app.include_router(chat_router)


@app.get("/api/health", response_model=HealthResponse)
def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    return HealthResponse(status="ok", provider=settings.llm_provider)
