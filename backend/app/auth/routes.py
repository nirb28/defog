"""Sign-up, email verification, login, Google sign-in, password reset and sessions."""

import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DB

from ..config import Settings, get_settings
from ..db import get_db
from ..emails import reset_email, send_email, verification_email
from ..models import AuthToken, LoginFailure, Session, User, utcnow
from ..security import (
    hash_password,
    hash_token,
    is_breached_password,
    new_token,
    password_needs_rehash,
    verify_password,
)
from . import google, oauth_state
from .schemas import (
    AuthOptions,
    AuthResponse,
    Client,
    ForgotPasswordRequest,
    LinkGoogleRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenRequest,
    UserOut,
)
from .sessions import (
    Auth,
    clear_session_cookie,
    require_auth,
    require_user,
    revoke_all_sessions,
    session_payload,
    set_session_cookie,
    start_session,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

VERIFY_EMAIL = "verify_email"
RESET_PASSWORD = "reset_password"
LINK_GOOGLE = "link_google"
MOBILE_LOGIN = "mobile_login"
LIFETIMES = {
    VERIFY_EMAIL: timedelta(hours=24),
    RESET_PASSWORD: timedelta(hours=1),
    LINK_GOOGLE: timedelta(minutes=15),
    MOBILE_LOGIN: timedelta(minutes=2),
}
RESEND_COOLDOWN = timedelta(seconds=60)

BREACHED_MESSAGE = "This password has appeared in a known data breach. Choose a different password."


def normalize_email(email: str) -> str:
    return email.strip().lower()


# --- Single-use tokens -----------------------------------------------------------------------


def issue_token(db: DB, user: User, purpose: str, data: dict | None = None) -> str:
    token = new_token()
    db.add(
        AuthToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=hash_token(token),
            data=data or {},
            expires_at=utcnow() + LIFETIMES[purpose],
        )
    )
    return token


def consume_token(db: DB, token: str, purpose: str) -> AuthToken | None:
    """Mark a token used and return it, or None if it's unknown, used, expired or the wrong kind.

    The row is locked so two simultaneous requests can't both use the same token.
    """
    row = db.scalar(
        select(AuthToken)
        .where(AuthToken.token_hash == hash_token(token), AuthToken.purpose == purpose)
        .with_for_update()
    )
    if row is None or row.used_at is not None or row.expires_at <= utcnow():
        return None
    row.used_at = utcnow()
    return row


def _recent_token_exists(db: DB, user: User, purpose: str) -> bool:
    return (
        db.scalar(
            select(func.count())
            .select_from(AuthToken)
            .where(
                AuthToken.user_id == user.id,
                AuthToken.purpose == purpose,
                AuthToken.created_at > utcnow() - RESEND_COOLDOWN,
            )
        )
        > 0
    )


def _send_verification(background: BackgroundTasks, settings: Settings, db: DB, user: User) -> None:
    token = issue_token(db, user, VERIFY_EMAIL)
    link = f"{settings.app_url}/verify-email?{urlencode({'token': token})}"
    subject, body = verification_email(settings, user.name, link)
    background.add_task(send_email, settings, user.email, subject, body)


# --- Responses ---------------------------------------------------------------------------------


def _signed_in(
    db: DB, settings: Settings, user: User, request: Request, response: Response, client: Client
) -> AuthResponse:
    issued = start_session(db, settings, user, request, client)
    db.commit()
    if issued.kind == "web":
        set_session_cookie(response, settings, issued.token)
    return AuthResponse(user=UserOut.of(user), **session_payload(issued))


@router.get("/options", response_model=AuthOptions)
def auth_options(settings: Settings = Depends(get_settings)) -> AuthOptions:
    return AuthOptions(google_enabled=settings.google_enabled)


# --- Registration and email verification ----------------------------------------------------


@router.post("/register", response_model=AuthResponse, status_code=201)
def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    email = normalize_email(body.email)
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists. Log in instead.",
        )
    if settings.password_breach_check and is_breached_password(body.password):
        raise HTTPException(status_code=422, detail=BREACHED_MESSAGE)

    user = User(
        name=body.name,
        email=email,
        password_hash=hash_password(body.password),
        school=body.school,
        year=body.year,
        major=body.major,
        time_zone=body.time_zone,
        consent_at=utcnow(),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        # Two sign-ups with the same email at the same moment.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists. Log in instead.",
        )
    _send_verification(background, settings, db, user)
    return _signed_in(db, settings, user, request, response, body.client)


@router.post("/verify-email", response_model=UserOut)
def verify_email(body: TokenRequest, db: DB = Depends(get_db)) -> UserOut:
    row = consume_token(db, body.token, VERIFY_EMAIL)
    if row is None:
        db.rollback()
        # Opening the same link twice (or a mail scanner opening it first) isn't an error
        # once the address is confirmed.
        used = db.scalar(
            select(AuthToken).where(
                AuthToken.token_hash == hash_token(body.token),
                AuthToken.purpose == VERIFY_EMAIL,
                AuthToken.used_at.is_not(None),
                AuthToken.expires_at > utcnow(),
            )
        )
        user = db.get(User, used.user_id) if used else None
        if user is not None and user.email_verified:
            return UserOut.of(user)
        raise HTTPException(
            status_code=400,
            detail="This confirmation link is invalid or has expired. Request a new one.",
        )
    user = db.get(User, row.user_id)
    assert user is not None
    user.email_verified = True
    db.commit()
    return UserOut.of(user)


@router.post("/resend-verification", status_code=204)
def resend_verification(
    background: BackgroundTasks,
    user: User = Depends(require_user),
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    if user.email_verified:
        raise HTTPException(status_code=400, detail="Your email is already confirmed.")
    if _recent_token_exists(db, user, VERIFY_EMAIL):
        raise HTTPException(
            status_code=429, detail="We just sent a link. Wait a minute before asking again."
        )
    _send_verification(background, settings, db, user)
    db.commit()
    return Response(status_code=204)


# --- Email + password login -------------------------------------------------------------------


def _check_login_limit(db: DB, settings: Settings, email: str) -> None:
    since = utcnow() - timedelta(minutes=settings.login_failure_window_minutes)
    failures = db.scalar(
        select(func.count())
        .select_from(LoginFailure)
        .where(LoginFailure.email == email, LoginFailure.created_at > since)
    )
    if failures >= settings.login_max_failures:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many failed attempts. Try again in {settings.login_failure_window_minutes} "
                "minutes, or reset your password."
            ),
        )


def _record_failure(db: DB, email: str) -> None:
    db.add(LoginFailure(email=email))
    db.commit()


@router.post("/login", response_model=AuthResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    email = normalize_email(body.email)
    _check_login_limit(db, settings, email)

    user = db.scalar(select(User).where(User.email == email))
    # Always run the hash check, so a missing account takes as long as a wrong password.
    password_ok = verify_password(user.password_hash if user else None, body.password)
    if user is None or not password_ok:
        _record_failure(db, email)
        if user is not None and user.password_hash is None:
            raise HTTPException(
                status_code=401,
                detail="This account uses Google sign-in. Use “Continue with Google”.",
            )
        raise HTTPException(status_code=401, detail="Incorrect email or password.")

    if password_needs_rehash(user.password_hash or ""):
        user.password_hash = hash_password(body.password)
    db.execute(delete(LoginFailure).where(LoginFailure.email == email))
    return _signed_in(db, settings, user, request, response, body.client)


@router.post("/logout", status_code=204)
def logout(
    auth: Auth = Depends(require_auth),
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    db.delete(auth.session)
    db.commit()
    response = Response(status_code=204)
    clear_session_cookie(response, settings)
    return response


@router.post("/logout-all", status_code=204)
def logout_all(
    auth: Auth = Depends(require_auth),
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    revoke_all_sessions(db, auth.user.id)
    db.commit()
    response = Response(status_code=204)
    clear_session_cookie(response, settings)
    return response


@router.post("/refresh", response_model=AuthResponse)
def refresh(
    body: RefreshRequest,
    request: Request,
    response: Response,
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    """Mobile only: swap a refresh token for a new access + refresh token pair."""
    session = db.scalar(
        select(Session)
        .where(
            Session.refresh_hash == hash_token(body.refresh_token),
            Session.kind == "mobile",
            Session.refresh_expires_at > utcnow(),
        )
        .with_for_update()
    )
    if session is None:
        raise HTTPException(status_code=401, detail="Your session has ended. Log in again.")
    user = db.get(User, session.user_id)
    db.delete(session)  # rotate: the old refresh token stops working
    assert user is not None
    return _signed_in(db, settings, user, request, response, "mobile")


# --- Password reset ------------------------------------------------------------------------------


@router.post("/forgot-password", status_code=202)
def forgot_password(
    body: ForgotPasswordRequest,
    background: BackgroundTasks,
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    # Same answer whether or not the account exists, so this can't be used to probe emails.
    user = db.scalar(select(User).where(User.email == normalize_email(body.email)))
    if user is not None and not _recent_token_exists(db, user, RESET_PASSWORD):
        token = issue_token(db, user, RESET_PASSWORD)
        db.commit()
        link = f"{settings.app_url}/reset-password?{urlencode({'token': token})}"
        subject, text = reset_email(settings, user.name, link)
        background.add_task(send_email, settings, user.email, subject, text)
    return {"detail": "If an account exists for that email, we've sent a reset link."}


@router.post("/reset-password", status_code=204)
def reset_password(
    body: ResetPasswordRequest,
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    row = consume_token(db, body.token, RESET_PASSWORD)
    if row is None:
        db.rollback()
        raise HTTPException(
            status_code=400, detail="This reset link is invalid or has expired. Request a new one."
        )
    if settings.password_breach_check and is_breached_password(body.password):
        db.rollback()  # leave the link usable so the student can try another password
        raise HTTPException(status_code=422, detail=BREACHED_MESSAGE)
    user = db.get(User, row.user_id)
    assert user is not None
    user.password_hash = hash_password(body.password)
    user.email_verified = True  # they proved they can read the inbox
    # A reset signs out every device, in case the old password was compromised.
    revoke_all_sessions(db, user.id)
    db.execute(delete(LoginFailure).where(LoginFailure.email == user.email))
    db.commit()
    return Response(status_code=204)


# --- Google sign-in ------------------------------------------------------------------------------


def _app_redirect(settings: Settings, path: str, **params: str) -> RedirectResponse:
    query = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(f"{settings.app_url}{path}{query}", status_code=303)


@router.get("/google/start")
def google_start(
    client: Client = Query("web"),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if not settings.google_enabled:
        raise HTTPException(status_code=404, detail="Google sign-in isn't set up on this server.")
    if client == "mobile" and not settings.mobile_redirect_uri:
        raise HTTPException(status_code=400, detail="Mobile Google sign-in isn't set up.")
    verifier, challenge = oauth_state.pkce_pair()
    nonce = secrets.token_urlsafe(24)
    response = RedirectResponse("about:blank", status_code=303)
    state = oauth_state.begin(
        response, settings, "google_login", verifier=verifier, nonce=nonce, client=client
    )
    response.headers["location"] = google.authorization_url(
        settings,
        redirect_path=google.LOGIN_CALLBACK,
        scope=google.LOGIN_SCOPES,
        state=state,
        code_challenge=challenge,
        nonce=nonce,
    )
    return response


def _google_failed(settings: Settings, response: RedirectResponse, reason: str) -> RedirectResponse:
    response.headers["location"] = f"{settings.app_url}/login?{urlencode({'error': reason})}"
    return response


@router.get("/google/callback")
def google_callback(
    request: Request,
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    response = RedirectResponse(settings.app_url, status_code=303)
    try:
        state = oauth_state.finish(request, response, settings, "google_login")
    except oauth_state.OAuthStateError as exc:
        log.info("Google sign-in state rejected: %s", exc)
        return _google_failed(settings, response, "google_expired")

    client: Client = "mobile" if state.get("client") == "mobile" else "web"
    code = request.query_params.get("code")
    if request.query_params.get("error") or not code:
        # Includes the student pressing Cancel on Google's screen.
        return _google_failed(settings, response, "google_cancelled")
    try:
        tokens = google.exchange_code(
            settings,
            code=code,
            redirect_path=google.LOGIN_CALLBACK,
            code_verifier=state["verifier"],
        )
        claims = google.verify_id_token(settings, tokens.get("id_token", ""), state["nonce"])
    except google.GoogleError as exc:
        log.warning("Google sign-in failed: %s", exc)
        return _google_failed(settings, response, "google_failed")

    sub, email = str(claims["sub"]), normalize_email(claims["email"])
    user = db.scalar(select(User).where(User.google_sub == sub))
    if user is None:
        existing = db.scalar(select(User).where(User.email == email))
        if existing is not None and existing.google_sub is not None:
            # The email belongs to an account already linked to a different Google login.
            return _google_failed(settings, response, "google_other_account")
        if existing is not None and existing.password_hash is not None:
            # Same email as a password account: confirm ownership with the password once.
            token = issue_token(db, existing, LINK_GOOGLE, {"google_sub": sub, "client": client})
            db.commit()
            response.headers["location"] = (
                f"{settings.app_url}/link-google?{urlencode({'token': token, 'email': email})}"
            )
            return response
        if existing is not None:
            user = existing
            user.google_sub = sub
            user.email_verified = True
        else:
            user = User(
                name=(claims.get("name") or email.split("@")[0])[:100],
                email=email,
                email_verified=True,  # Google has verified it
                google_sub=sub,
            )
            db.add(user)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            return _google_failed(settings, response, "google_failed")

    return _finish_google_login(db, settings, request, response, user, client)


def _finish_google_login(
    db: DB,
    settings: Settings,
    request: Request,
    response: RedirectResponse,
    user: User,
    client: Client,
) -> RedirectResponse:
    if client == "mobile":
        # The app can't read our cookies; give it a 2-minute code to swap for tokens.
        code = issue_token(db, user, MOBILE_LOGIN)
        db.commit()
        separator = "&" if "?" in settings.mobile_redirect_uri else "?"
        response.headers["location"] = (
            f"{settings.mobile_redirect_uri}{separator}{urlencode({'code': code})}"
        )
        return response
    issued = start_session(db, settings, user, request, "web")
    db.commit()
    set_session_cookie(response, settings, issued.token)
    # New Google users still need to fill in school, time zone and consent.
    response.headers["location"] = (
        f"{settings.app_url}/" if user.profile_complete else f"{settings.app_url}/complete-profile"
    )
    return response


@router.post("/google/link", response_model=AuthResponse)
def google_link(
    body: LinkGoogleRequest,
    request: Request,
    response: Response,
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    """Confirm, with the account password, that a Google login may be linked to it."""
    row = db.scalar(
        select(AuthToken).where(
            AuthToken.token_hash == hash_token(body.token), AuthToken.purpose == LINK_GOOGLE
        )
    )
    if row is None or row.used_at is not None or row.expires_at <= utcnow():
        raise HTTPException(
            status_code=400, detail="This link has expired. Try “Continue with Google” again."
        )
    user = db.get(User, row.user_id)
    assert user is not None
    _check_login_limit(db, settings, user.email)
    if not verify_password(user.password_hash, body.password):
        _record_failure(db, user.email)
        raise HTTPException(status_code=401, detail="Incorrect password.")

    row = consume_token(db, body.token, LINK_GOOGLE)
    if row is None:
        db.rollback()
        raise HTTPException(status_code=400, detail="This link was already used.")
    sub = row.data["google_sub"]
    if db.scalar(select(User.id).where(User.google_sub == sub, User.id != user.id)):
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="That Google account is already linked to another Defog account.",
        )
    user.google_sub = sub
    user.email_verified = True
    db.execute(delete(LoginFailure).where(LoginFailure.email == user.email))
    return _signed_in(db, settings, user, request, response, body.client)


@router.post("/mobile/exchange", response_model=AuthResponse)
def mobile_exchange(
    body: TokenRequest,
    request: Request,
    response: Response,
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    """Mobile Google sign-in, last step: swap the one-time code for tokens."""
    row = consume_token(db, body.token, MOBILE_LOGIN)
    if row is None:
        db.rollback()
        raise HTTPException(status_code=400, detail="This sign-in code is invalid or has expired.")
    user = db.get(User, row.user_id)
    assert user is not None
    return _signed_in(db, settings, user, request, response, "mobile")
