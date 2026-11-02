"""Connections page: connect, configure, sync and disconnect Canvas and Google Calendar."""

import logging
import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DB

from ..auth import google, oauth_state
from ..auth.sessions import require_ready_user
from ..config import Settings, get_settings
from ..db import get_db
from ..models import Connection, Course, User
from . import canvas, gcal
from .sync import sync_connection
from .tokens import (
    CANVAS,
    GOOGLE_CALENDAR,
    NeedsAttention,
    access_token,
    mark_needs_attention,
    read_tokens,
    revoke,
    store_tokens,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/connections", tags=["connections"])


# --- Shapes ------------------------------------------------------------------------------------


class ConnectionOut(BaseModel):
    provider: Literal["canvas", "google_calendar"]
    id: uuid.UUID | None = None
    # not_connected | syncing (first sync running) | connected | needs_attention
    status: str
    status_detail: str | None = None
    auth_type: str | None = None
    base_url: str | None = None
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None
    calendar_ids: list[str] | None = None
    allow_writes: bool | None = None
    can_write: bool | None = None  # the student granted the events scope
    course_count: int | None = None


class ConnectionsOut(BaseModel):
    connections: list[ConnectionOut]
    canvas_oauth_schools: list[str]
    google_enabled: bool


class CanvasTokenRequest(BaseModel):
    base_url: str = Field(min_length=3, max_length=255)
    token: str = Field(min_length=10, max_length=500)


class ConnectionUpdate(BaseModel):
    calendar_ids: list[str] | None = Field(default=None, min_length=1, max_length=20)
    allow_writes: bool | None = None


class CalendarOut(BaseModel):
    id: str
    name: str
    primary: bool
    selected: bool


def _out(db: DB, conn: Connection | None, provider: str) -> ConnectionOut:
    if conn is None:
        return ConnectionOut(provider=provider, status="not_connected")  # type: ignore[arg-type]
    status = conn.status
    if status == "connected" and conn.last_synced_at is None and conn.last_sync_error is None:
        status = "syncing"
    out = ConnectionOut(
        provider=conn.provider,  # type: ignore[arg-type]
        id=conn.id,
        status=status,
        status_detail=conn.status_detail,
        auth_type=conn.auth_type,
        base_url=conn.base_url,
        last_synced_at=conn.last_synced_at,
        last_sync_error=conn.last_sync_error,
    )
    if conn.provider == GOOGLE_CALENDAR:
        out.calendar_ids = (conn.settings or {}).get("calendar_ids") or ["primary"]
        out.allow_writes = bool((conn.settings or {}).get("allow_writes"))
        out.can_write = gcal.WRITE_SCOPE in conn.scopes.split()
    else:
        out.course_count = db.scalar(
            select(func.count()).select_from(Course).where(Course.connection_id == conn.id)
        )
    return out


def _get_owned(db: DB, user: User, connection_id: uuid.UUID) -> Connection:
    conn = db.scalar(
        select(Connection).where(Connection.id == connection_id, Connection.user_id == user.id)
    )
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found.")
    return conn


def _by_provider(db: DB, user: User, provider: str) -> Connection | None:
    return db.scalar(
        select(Connection).where(Connection.user_id == user.id, Connection.provider == provider)
    )


def _save_connection(
    db: DB,
    user: User,
    provider: str,
    *,
    auth_type: str,
    tokens: dict,
    expires_in: int | None,
    scopes: str,
    base_url: str | None = None,
    settings: dict | None = None,
) -> Connection:
    """Create the connection, or replace the credentials of an existing one (reconnect)."""
    conn = _by_provider(db, user, provider)
    if conn is not None and (conn.base_url != base_url):
        # Switching schools: drop the old school's data entirely.
        revoke(conn)
        db.delete(conn)
        db.flush()
        conn = None
    if conn is None:
        conn = Connection(user_id=user.id, provider=provider, settings=settings or {})
        db.add(conn)
    elif settings:
        conn.settings = {**(conn.settings or {}), **settings}
    conn.auth_type = auth_type
    conn.base_url = base_url
    conn.scopes = scopes
    conn.status = "connected"
    conn.status_detail = None
    conn.last_sync_error = None
    store_tokens(conn, tokens, expires_in)
    db.flush()
    return conn


def _redirect(settings: Settings, **params: str) -> RedirectResponse:
    return RedirectResponse(f"{settings.app_url}/connections?{urlencode(params)}", status_code=303)


# --- Listing -----------------------------------------------------------------------------------


@router.get("", response_model=ConnectionsOut)
def list_connections(
    user: User = Depends(require_ready_user),
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ConnectionsOut:
    return ConnectionsOut(
        connections=[_out(db, _by_provider(db, user, p), p) for p in (CANVAS, GOOGLE_CALENDAR)],
        canvas_oauth_schools=sorted(settings.canvas_oauth_clients),
        google_enabled=settings.google_enabled,
    )


# --- Canvas ------------------------------------------------------------------------------------


def _canvas_url(raw: str, settings: Settings) -> str:
    try:
        base_url = canvas.normalize_base_url(raw, settings)
        canvas.assert_public_host(base_url)
    except canvas.InvalidCanvasURL as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return base_url


@router.post("/canvas/token", response_model=ConnectionOut)
def connect_canvas_token(
    body: CanvasTokenRequest,
    background: BackgroundTasks,
    user: User = Depends(require_ready_user),
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ConnectionOut:
    """Fallback for schools without a developer key: the student pastes a personal token."""
    base_url = _canvas_url(body.base_url, settings)
    token = body.token.strip()
    try:
        with canvas.CanvasAPI(base_url, token) as api:
            profile = api.self_profile()
    except canvas.CanvasAuthError:
        raise HTTPException(
            status_code=422,
            detail=(
                "Canvas didn't accept that token. Check you copied all of it. Some schools turn "
                "off personal access tokens; if so, Defog can't connect to your Canvas this way."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log.info("Canvas token check failed for %s: %s", base_url, exc)
        raise HTTPException(
            status_code=502,
            detail="We couldn't reach Canvas at that address. Check it and try again.",
        )
    conn = _save_connection(
        db,
        user,
        CANVAS,
        auth_type="token",
        tokens={"access_token": token},
        expires_in=None,
        scopes="",
        base_url=base_url,
        settings={"user_name": profile.get("name")},
    )
    conn.last_synced_at = None
    db.commit()
    background.add_task(sync_connection, conn.id)
    return _out(db, conn, CANVAS)


@router.get("/canvas/start")
def canvas_start(
    base_url: str = Query(..., min_length=3, max_length=255),
    user: User = Depends(require_ready_user),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    url = _canvas_url(base_url, settings)
    if settings.canvas_client_for(url) is None:
        raise HTTPException(
            status_code=400,
            detail="Defog can't sign in to this school's Canvas yet. Use an access token instead.",
        )
    response = RedirectResponse("about:blank", status_code=303)
    state = oauth_state.begin(response, settings, "canvas", user_id=str(user.id), base_url=url)
    response.headers["location"] = canvas.authorization_url(settings, url, state)
    return response


@router.get("/canvas/callback")
def canvas_callback(
    request: Request,
    background: BackgroundTasks,
    user: User = Depends(require_ready_user),
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    response = _redirect(settings, connected="canvas")
    try:
        state = oauth_state.finish(request, response, settings, "canvas")
    except oauth_state.OAuthStateError:
        response.headers["location"] = _redirect(settings, error="canvas_expired").headers[
            "location"
        ]
        return response
    code = request.query_params.get("code")
    if state.get("user_id") != str(user.id) or request.query_params.get("error") or not code:
        response.headers["location"] = _redirect(settings, error="canvas_cancelled").headers[
            "location"
        ]
        return response
    try:
        data = canvas.exchange_code(settings, state["base_url"], code)
    except canvas.CanvasError as exc:
        log.warning("Canvas code exchange failed: %s", exc)
        response.headers["location"] = _redirect(settings, error="canvas_failed").headers[
            "location"
        ]
        return response
    conn = _save_connection(
        db,
        user,
        CANVAS,
        auth_type="oauth",
        tokens={"access_token": data["access_token"], "refresh_token": data.get("refresh_token")},
        expires_in=data.get("expires_in"),
        scopes="",
        base_url=state["base_url"],
        settings={"user_name": (data.get("user") or {}).get("name")},
    )
    conn.last_synced_at = None
    db.commit()
    background.add_task(sync_connection, conn.id)
    return response


# --- Google Calendar ---------------------------------------------------------------------------


@router.get("/google-calendar/start")
def google_calendar_start(
    write: bool = Query(False),
    user: User = Depends(require_ready_user),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if not settings.google_enabled:
        raise HTTPException(status_code=404, detail="Google Calendar isn't set up on this server.")
    verifier, challenge = oauth_state.pkce_pair()
    response = RedirectResponse("about:blank", status_code=303)
    state = oauth_state.begin(
        response, settings, "google_calendar", user_id=str(user.id), verifier=verifier, write=write
    )
    response.headers["location"] = google.authorization_url(
        settings,
        redirect_path=gcal.OAUTH_CALLBACK,
        scope=gcal.scopes_for(write),
        state=state,
        code_challenge=challenge,
        offline=True,
        login_hint=user.email,
    )
    return response


@router.get("/google-calendar/callback")
def google_calendar_callback(
    request: Request,
    background: BackgroundTasks,
    user: User = Depends(require_ready_user),
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    response = _redirect(settings, connected="google_calendar")

    def fail(reason: str) -> RedirectResponse:
        response.headers["location"] = _redirect(settings, error=reason).headers["location"]
        return response

    try:
        state = oauth_state.finish(request, response, settings, "google_calendar")
    except oauth_state.OAuthStateError:
        return fail("google_calendar_expired")
    code = request.query_params.get("code")
    if state.get("user_id") != str(user.id) or request.query_params.get("error") or not code:
        return fail("google_calendar_cancelled")
    try:
        data = google.exchange_code(
            settings, code=code, redirect_path=gcal.OAUTH_CALLBACK, code_verifier=state["verifier"]
        )
    except google.GoogleError as exc:
        log.warning("Google Calendar code exchange failed: %s", exc)
        return fail("google_calendar_failed")

    granted = set((data.get("scope") or "").split())
    if gcal.READ_SCOPE not in granted:
        # Google lets students untick permissions on the consent screen.
        return fail("google_calendar_scope")

    existing = _by_provider(db, user, GOOGLE_CALENDAR)
    refresh_token = data.get("refresh_token")
    if not refresh_token and existing is not None:
        # Google skips the refresh token when the student had already consented; keep ours.
        try:
            refresh_token = read_tokens(existing).get("refresh_token")
        except ValueError:
            refresh_token = None
    if not refresh_token:
        return fail("google_calendar_failed")

    wants_writes = bool(state.get("write")) and gcal.WRITE_SCOPE in granted
    conn_settings = {"allow_writes": wants_writes}
    if existing is None:
        conn_settings["calendar_ids"] = ["primary"]
    conn = _save_connection(
        db,
        user,
        GOOGLE_CALENDAR,
        auth_type="oauth",
        tokens={"access_token": data["access_token"], "refresh_token": refresh_token},
        expires_in=data.get("expires_in"),
        scopes=" ".join(sorted(granted)),
        settings=conn_settings,
    )
    conn.last_synced_at = None
    db.commit()
    background.add_task(sync_connection, conn.id)
    return response


@router.get("/{connection_id}/calendars", response_model=list[CalendarOut])
def list_calendars(
    connection_id: uuid.UUID,
    user: User = Depends(require_ready_user),
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[CalendarOut]:
    conn = _get_owned(db, user, connection_id)
    if conn.provider != GOOGLE_CALENDAR:
        raise HTTPException(status_code=400, detail="Only Google Calendar has calendars to pick.")
    try:
        token = access_token(db, conn, settings)
        with gcal.CalendarAPI(token) as api:
            calendars = api.calendars()
    except NeedsAttention as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except gcal.CalendarAuthError:
        mark_needs_attention(db, conn)
        raise HTTPException(status_code=409, detail=conn.status_detail)
    except Exception as exc:  # noqa: BLE001
        log.warning("calendarList failed: %s", exc)
        raise HTTPException(status_code=502, detail="Couldn't load your calendars. Try again.")
    selected = set((conn.settings or {}).get("calendar_ids") or ["primary"])
    out = []
    for c in calendars:
        primary = bool(c.get("primary"))
        out.append(
            CalendarOut(
                id=c["id"],
                name=c.get("summaryOverride") or c.get("summary") or c["id"],
                primary=primary,
                selected=c["id"] in selected or (primary and "primary" in selected),
            )
        )
    out.sort(key=lambda c: (not c.primary, c.name.lower()))
    return out


@router.patch("/{connection_id}", response_model=ConnectionOut)
def update_connection(
    connection_id: uuid.UUID,
    body: ConnectionUpdate,
    background: BackgroundTasks,
    user: User = Depends(require_ready_user),
    db: DB = Depends(get_db),
) -> ConnectionOut:
    conn = _get_owned(db, user, connection_id)
    if conn.provider != GOOGLE_CALENDAR:
        raise HTTPException(status_code=400, detail="Canvas has no settings to change.")
    new_settings = dict(conn.settings or {})
    resync = False
    if body.calendar_ids is not None:
        ids = list(dict.fromkeys(cid.strip() for cid in body.calendar_ids if cid.strip()))
        if not ids or any(len(cid) > 255 for cid in ids):
            raise HTTPException(status_code=422, detail="Pick at least one calendar.")
        new_settings["calendar_ids"] = ids
        resync = True
    if body.allow_writes is not None:
        if body.allow_writes and gcal.WRITE_SCOPE not in conn.scopes.split():
            raise HTTPException(
                status_code=409,
                detail="Defog needs permission to add events. Reconnect with adding events turned on.",
            )
        new_settings["allow_writes"] = body.allow_writes
    conn.settings = new_settings
    db.commit()
    if resync:
        background.add_task(sync_connection, conn.id)
    return _out(db, conn, conn.provider)


# --- Sync and disconnect ------------------------------------------------------------------------


@router.post("/{connection_id}/sync", response_model=ConnectionOut)
def sync_now(
    connection_id: uuid.UUID,
    user: User = Depends(require_ready_user),
    db: DB = Depends(get_db),
) -> ConnectionOut:
    conn = _get_owned(db, user, connection_id)
    if conn.status == "needs_attention":
        raise HTTPException(status_code=409, detail=conn.status_detail or "Reconnect this source.")
    db.commit()  # don't hold a transaction open while the sync runs
    result = sync_connection(conn.id)
    db.expire_all()
    conn = _get_owned(db, user, connection_id)
    if not result.ok and not result.skipped:
        raise HTTPException(
            status_code=502 if conn.status == "connected" else 409, detail=result.error
        )
    return _out(db, conn, conn.provider)


@router.delete("/{connection_id}", status_code=204)
def disconnect(
    connection_id: uuid.UUID,
    user: User = Depends(require_ready_user),
    db: DB = Depends(get_db),
) -> None:
    conn = _get_owned(db, user, connection_id)
    revoke(conn)
    db.delete(conn)  # cascades to courses, assignments, announcements and events
    db.commit()
