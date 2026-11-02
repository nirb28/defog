"""Signed-in sessions: an HttpOnly cookie on the web, access + refresh tokens on mobile."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DB

from ..config import Settings
from ..db import get_db
from ..models import Session, User, utcnow
from ..security import hash_token, new_token, tokens_match

SESSION_COOKIE = "defog_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Don't write to the database on every request just to bump last_used_at.
TOUCH_INTERVAL = timedelta(minutes=5)


@dataclass
class IssuedSession:
    kind: Literal["web", "mobile"]
    token: str
    csrf_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None


def _device(request: Request) -> str | None:
    agent = request.headers.get("user-agent")
    return agent[:200] if agent else None


def start_session(
    db: DB,
    settings: Settings,
    user: User,
    request: Request,
    kind: Literal["web", "mobile"],
) -> IssuedSession:
    """Create a session row. The caller commits and then delivers it with deliver_session()."""
    now = utcnow()
    token = new_token()
    if kind == "web":
        csrf = new_token()
        db.add(
            Session(
                user_id=user.id,
                kind="web",
                token_hash=hash_token(token),
                expires_at=now + timedelta(days=settings.session_days),
                csrf_token=csrf,
                device=_device(request),
            )
        )
        return IssuedSession(kind="web", token=token, csrf_token=csrf)

    refresh = new_token()
    db.add(
        Session(
            user_id=user.id,
            kind="mobile",
            token_hash=hash_token(token),
            expires_at=now + timedelta(minutes=settings.access_token_minutes),
            refresh_hash=hash_token(refresh),
            refresh_expires_at=now + timedelta(days=settings.refresh_token_days),
            device=_device(request),
        )
    )
    return IssuedSession(
        kind="mobile",
        token=token,
        refresh_token=refresh,
        expires_in=settings.access_token_minutes * 60,
    )


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_days * 24 * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, secure=settings.cookie_secure, samesite="lax"
    )


def session_payload(issued: IssuedSession) -> dict:
    """What the client gets back. The web session token itself only travels in the cookie."""
    if issued.kind == "web":
        return {"csrf_token": issued.csrf_token}
    return {
        "access_token": issued.token,
        "refresh_token": issued.refresh_token,
        "token_type": "bearer",
        "expires_in": issued.expires_in,
    }


def revoke_all_sessions(db: DB, user_id) -> None:
    db.execute(delete(Session).where(Session.user_id == user_id))


# --- Request authentication ---------------------------------------------------------------


@dataclass
class Auth:
    user: User
    session: Session


def _find_session(db: DB, token: str, kind: str) -> Session | None:
    return db.scalar(
        select(Session).where(
            Session.token_hash == hash_token(token),
            Session.kind == kind,
            Session.expires_at > utcnow(),
        )
    )


def optional_auth(request: Request, db: DB = Depends(get_db)) -> Auth | None:
    header = request.headers.get("authorization", "")
    session: Session | None = None
    if header.lower().startswith("bearer "):
        session = _find_session(db, header[7:].strip(), "mobile")
        if session is None:
            return None
    else:
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie:
            session = _find_session(db, cookie, "web")
        if session is None:
            return None
        # Cookies are sent automatically by the browser, so state-changing requests must
        # also carry the CSRF token the app received at login (a page on another site
        # can't read it).
        if request.method not in SAFE_METHODS and not tokens_match(
            request.headers.get(CSRF_HEADER), session.csrf_token
        ):
            raise HTTPException(
                status_code=403, detail="Missing or invalid CSRF token. Reload the page."
            )

    user = db.get(User, session.user_id)
    if user is None:
        return None
    now = utcnow()
    if now - session.last_used_at > TOUCH_INTERVAL:
        session.last_used_at = now
        db.commit()
    return Auth(user=user, session=session)


def require_auth(auth: Auth | None = Depends(optional_auth)) -> Auth:
    if auth is None:
        raise HTTPException(
            status_code=401,
            detail="You're not logged in.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return auth


def require_user(auth: Auth = Depends(require_auth)) -> User:
    return auth.user


def require_ready_user(user: User = Depends(require_user)) -> User:
    """Connections and chat need a verified email and a finished profile."""
    if not user.email_verified:
        raise HTTPException(status_code=403, detail="Confirm your email address to continue.")
    if not user.profile_complete:
        raise HTTPException(status_code=403, detail="Finish your profile to continue.")
    return user
