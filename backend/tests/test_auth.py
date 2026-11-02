"""Requirement 1: registration, email verification, login, Google sign-in, sessions."""

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import func, select, update

from app import emails
from app.models import AuthToken, Connection, LoginFailure, Message, Session, User, utcnow
from tests.conftest import (
    PASSWORD,
    connect_canvas,
    connect_google_calendar,
    last_email_link,
    new_client,
    register,
)

FORM = {
    "name": "Ben Ortiz",
    "email": "ben@school.edu",
    "password": PASSWORD,
    "school": "Penn",
    "time_zone": "America/Chicago",
    "accept_terms": True,
}


# --- Registration form ---------------------------------------------------------------------


def test_signup_verify_and_land_on_dashboard(client):
    res = client.post("/api/auth/register", json={**FORM, "year": "Senior", "major": "Chemistry"})
    assert res.status_code == 201
    body = res.json()
    assert body["user"]["email_verified"] is False
    assert body["user"]["profile_complete"] is True
    assert body["user"]["consent_at"] is not None
    assert body["csrf_token"]
    assert body["access_token"] is None  # web sessions travel only in the cookie
    assert "defog_session" in res.cookies
    # The session cookie is HttpOnly and SameSite=Lax.
    cookie_header = res.headers["set-cookie"].lower()
    assert "httponly" in cookie_header and "samesite=lax" in cookie_header

    # Not verified yet: the dashboard features are locked.
    assert client.get("/api/connections").status_code == 403
    assert "Confirm your email" in client.get("/api/connections").json()["detail"]

    assert len(emails.outbox) == 1 and emails.outbox[0]["To"] == "ben@school.edu"
    token = last_email_link("/verify-email")
    assert (
        client.post("/api/auth/verify-email", json={"token": token}).json()["email_verified"]
        is True
    )
    assert client.get("/api/connections").status_code == 200
    # Opening the link again is harmless; a made-up token is not accepted.
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    assert client.post("/api/auth/verify-email", json={"token": "x" * 43}).status_code == 400


def test_email_is_case_insensitive_and_duplicates_point_to_login(client):
    assert client.post("/api/auth/register", json=FORM).status_code == 201
    res = new_client().post("/api/auth/register", json={**FORM, "email": "  BEN@School.EDU "})
    assert res.status_code == 409
    assert "Log in" in res.json()["detail"]


def test_required_fields_and_validation(client):
    for missing in ("name", "email", "school", "time_zone", "accept_terms"):
        body = {k: v for k, v in FORM.items() if k != missing}
        assert client.post("/api/auth/register", json=body).status_code == 422, missing
    bad = [
        {"password": "short"},
        {"email": "not-an-email"},
        {"time_zone": "Mars/Olympus"},
        {"accept_terms": False},
        {"name": "   "},
        {"name": "x" * 101},
        {"year": "Fifth"},
    ]
    for change in bad:
        assert client.post("/api/auth/register", json={**FORM, **change}).status_code == 422, change


def test_breached_password_is_rejected(client, internet):
    res = client.post("/api/auth/register", json={**FORM, "password": "password123"})
    assert res.status_code == 422
    assert "breach" in res.json()["detail"]
    # Only the 5-character hash prefix left the server.
    hibp = [u for u in internet.requests if "pwnedpasswords" in u]
    assert hibp and all(len(u.rsplit("/", 1)[-1]) == 5 for u in hibp)


def test_signup_still_works_if_breach_service_is_down(client, internet):
    internet.hibp_down = True
    assert client.post("/api/auth/register", json=FORM).status_code == 201


def test_resend_verification_is_rate_limited(client, db):
    student = register(client, verify=False)
    assert student.post("/api/auth/resend-verification").status_code == 429  # just sent at signup
    db.execute(update(AuthToken).values(created_at=utcnow() - timedelta(minutes=2)))
    db.commit()
    assert student.post("/api/auth/resend-verification").status_code == 204
    assert len(emails.outbox) == 2


# --- Login and rate limiting ---------------------------------------------------------------


def test_login_with_password(client):
    register(client, email="ann@school.edu")
    other = new_client()
    res = other.post("/api/auth/login", json={"email": "ANN@school.edu", "password": PASSWORD})
    assert res.status_code == 200
    assert res.json()["user"]["email"] == "ann@school.edu"
    assert other.get("/api/me").status_code == 200


def test_wrong_password_and_unknown_email_look_the_same(client):
    register(client)
    a = new_client().post(
        "/api/auth/login", json={"email": "ann@school.edu", "password": "wrong-password"}
    )
    b = new_client().post(
        "/api/auth/login", json={"email": "nobody@school.edu", "password": "wrong-password"}
    )
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_five_failures_in_15_minutes_locks_the_account(client, db):
    register(client)
    c = new_client()
    for _ in range(5):
        assert (
            c.post(
                "/api/auth/login", json={"email": "ann@school.edu", "password": "nope-nope"}
            ).status_code
            == 401
        )
    res = c.post("/api/auth/login", json={"email": "ann@school.edu", "password": PASSWORD})
    assert res.status_code == 429
    # Once the failures are older than 15 minutes, the right password works again.
    db.execute(update(LoginFailure).values(created_at=utcnow() - timedelta(minutes=16)))
    db.commit()
    assert (
        c.post(
            "/api/auth/login", json={"email": "ann@school.edu", "password": PASSWORD}
        ).status_code
        == 200
    )
    assert db.scalar(select(func.count()).select_from(LoginFailure)) == 0


def test_csrf_token_required_for_cookie_requests(client):
    student = register(client)
    assert client.post("/api/auth/logout").status_code == 403
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert student.post("/api/auth/logout").status_code == 204
    assert client.get("/api/me").status_code == 401


def test_me_returns_csrf_token_for_page_reloads(client):
    student = register(client)
    assert client.get("/api/me").json()["csrf_token"] == student.csrf


# --- Mobile tokens -------------------------------------------------------------------------------


def test_mobile_login_access_and_refresh_tokens(client, db):
    register(client)
    mobile = new_client()
    res = mobile.post(
        "/api/auth/login",
        json={"email": "ann@school.edu", "password": PASSWORD, "client": "mobile"},
    )
    body = res.json()
    assert body["access_token"] and body["refresh_token"] and body["expires_in"] == 3600
    assert "defog_session" not in res.cookies
    auth = {"Authorization": f"Bearer {body['access_token']}"}
    assert mobile.get("/api/me", headers=auth).status_code == 200
    # Bearer requests don't need CSRF tokens (nothing is sent automatically by a browser).
    assert (
        mobile.patch("/api/me", json={"major": "Physics"}, headers=auth).json()["major"]
        == "Physics"
    )

    refreshed = mobile.post(
        "/api/auth/refresh", json={"refresh_token": body["refresh_token"]}
    ).json()
    assert refreshed["access_token"] != body["access_token"]
    assert mobile.get("/api/me", headers=auth).status_code == 401  # old access token is gone
    assert (
        mobile.get(
            "/api/me", headers={"Authorization": f"Bearer {refreshed['access_token']}"}
        ).status_code
        == 200
    )
    # Refresh tokens rotate: the old one can't be replayed.
    assert (
        mobile.post("/api/auth/refresh", json={"refresh_token": body["refresh_token"]}).status_code
        == 401
    )

    # Access tokens are short-lived.
    db.execute(update(Session).values(expires_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    assert (
        mobile.get(
            "/api/me", headers={"Authorization": f"Bearer {refreshed['access_token']}"}
        ).status_code
        == 401
    )


def test_tokens_are_stored_hashed(client, db):
    register(client)
    cookie = client.cookies.get("defog_session")
    hashes = db.scalars(select(Session.token_hash)).all()
    assert cookie not in hashes and len(hashes[0]) == 64


def test_log_out_of_all_devices(client):
    student = register(client)
    mobile = new_client()
    token = mobile.post(
        "/api/auth/login",
        json={"email": "ann@school.edu", "password": PASSWORD, "client": "mobile"},
    ).json()["access_token"]
    laptop = new_client()
    laptop.post("/api/auth/login", json={"email": "ann@school.edu", "password": PASSWORD})
    assert student.post("/api/auth/logout-all").status_code == 204
    assert client.get("/api/me").status_code == 401
    assert laptop.get("/api/me").status_code == 401
    assert mobile.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


# --- Password reset ----------------------------------------------------------------------------------


def test_forgot_password_never_reveals_accounts(client):
    res = client.post("/api/auth/forgot-password", json={"email": "ghost@school.edu"})
    assert res.status_code == 202 and emails.outbox == []


def test_reset_password_flow(client, db):
    register(client)
    emails.outbox.clear()
    other = new_client()
    assert (
        other.post("/api/auth/forgot-password", json={"email": "ann@school.edu"}).status_code == 202
    )
    token = last_email_link("/reset-password")
    # Breached passwords are refused and the link stays usable.
    assert (
        other.post(
            "/api/auth/reset-password", json={"token": token, "password": "iloveyou1"}
        ).status_code
        == 422
    )
    assert (
        other.post(
            "/api/auth/reset-password", json={"token": token, "password": "brand-new-pass-99"}
        ).status_code
        == 204
    )
    assert client.get("/api/me").status_code == 401  # every session was signed out
    assert (
        other.post(
            "/api/auth/reset-password", json={"token": token, "password": "another-pass-99"}
        ).status_code
        == 400
    )
    assert (
        other.post(
            "/api/auth/login", json={"email": "ann@school.edu", "password": PASSWORD}
        ).status_code
        == 401
    )
    assert (
        other.post(
            "/api/auth/login", json={"email": "ann@school.edu", "password": "brand-new-pass-99"}
        ).status_code
        == 200
    )


def test_reset_link_expires_after_an_hour(client, db):
    register(client)
    client.post("/api/auth/forgot-password", json={"email": "ann@school.edu"})
    token = last_email_link("/reset-password")
    row = db.scalar(select(AuthToken).where(AuthToken.purpose == "reset_password"))
    assert abs((row.expires_at - row.created_at) - timedelta(hours=1)) < timedelta(seconds=5)
    db.execute(update(AuthToken).values(expires_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    assert (
        client.post(
            "/api/auth/reset-password", json={"token": token, "password": "brand-new-pass-99"}
        ).status_code
        == 400
    )


# --- Google sign-in --------------------------------------------------------------------------------


def _google_start(client, mobile: bool = False):
    res = client.get(
        "/api/auth/google/start" + ("?client=mobile" if mobile else ""), follow_redirects=False
    )
    assert res.status_code == 303
    url = urlsplit(res.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    return url, q


def test_google_start_uses_only_login_scopes_and_pkce(client):
    url, q = _google_start(client)
    assert url.netloc == "accounts.google.com"
    assert q["scope"] == "openid email profile"  # no Calendar scopes at login
    assert q["code_challenge_method"] == "S256" and q["code_challenge"]
    assert q["redirect_uri"] == "http://localhost:5173/api/auth/google/callback"
    assert q["state"] and q["nonce"]


def test_google_signup_creates_account_then_asks_for_profile(client, internet):
    _, q = _google_start(client)
    code = internet.google.login_code(
        sub="g-1", email="New.Student@gmail.com", name="New Student", nonce=q["nonce"]
    )
    res = client.get(
        f"/api/auth/google/callback?code={code}&state={q['state']}", follow_redirects=False
    )
    assert res.status_code == 303
    assert res.headers["location"] == "http://localhost:5173/complete-profile"
    assert internet.google.last_token_request["code_verifier"]  # PKCE verifier was sent

    me = client.get("/api/me").json()
    user = me["user"]
    assert user["email"] == "new.student@gmail.com" and user["email_verified"] is True
    assert user["has_password"] is False and user["google_linked"] is True
    assert user["profile_complete"] is False
    headers = {"X-CSRF-Token": me["csrf_token"]}
    assert client.get("/api/connections").json()["detail"] == "Finish your profile to continue."

    res = client.patch(
        "/api/me",
        headers=headers,
        json={"school": "Penn", "time_zone": "America/New_York", "accept_terms": True},
    )
    assert res.json()["profile_complete"] is True
    assert client.get("/api/connections").status_code == 200


def test_google_login_for_existing_password_account_needs_password_once(client, internet, db):
    register(client)
    browser = new_client()
    _, q = _google_start(browser)
    code = internet.google.login_code(sub="g-ann", email="ann@school.edu", nonce=q["nonce"])
    res = browser.get(
        f"/api/auth/google/callback?code={code}&state={q['state']}", follow_redirects=False
    )
    location = urlsplit(res.headers["location"])
    assert location.path == "/link-google"
    token = parse_qs(location.query)["token"][0]
    assert browser.get("/api/me").status_code == 401  # not signed in yet

    assert (
        browser.post(
            "/api/auth/google/link", json={"token": token, "password": "wrong-one"}
        ).status_code
        == 401
    )
    res = browser.post("/api/auth/google/link", json={"token": token, "password": PASSWORD})
    assert res.status_code == 200 and res.json()["user"]["google_linked"] is True
    assert (
        browser.post(
            "/api/auth/google/link", json={"token": token, "password": PASSWORD}
        ).status_code
        == 400
    )

    # From now on, either method works.
    third = new_client()
    _, q = _google_start(third)
    code = internet.google.login_code(sub="g-ann", email="ann@school.edu", nonce=q["nonce"])
    res = third.get(
        f"/api/auth/google/callback?code={code}&state={q['state']}", follow_redirects=False
    )
    assert res.headers["location"] == "http://localhost:5173/"
    assert third.get("/api/me").json()["user"]["email"] == "ann@school.edu"
    assert (
        new_client()
        .post("/api/auth/login", json={"email": "ann@school.edu", "password": PASSWORD})
        .status_code
        == 200
    )
    assert db.scalar(select(func.count()).select_from(User)) == 1


def test_google_callback_rejects_bad_state_nonce_and_unverified_email(client, internet):
    _, q = _google_start(client)
    code = internet.google.login_code(sub="g-2", email="x@gmail.com", nonce=q["nonce"])
    res = client.get(f"/api/auth/google/callback?code={code}&state=forged", follow_redirects=False)
    assert "error=google_expired" in res.headers["location"]

    _, q = _google_start(client)
    code = internet.google.login_code(sub="g-2", email="x@gmail.com", nonce="someone-elses")
    res = client.get(
        f"/api/auth/google/callback?code={code}&state={q['state']}", follow_redirects=False
    )
    assert "error=google_failed" in res.headers["location"]

    _, q = _google_start(client)
    code = internet.google.login_code(
        sub="g-2", email="x@gmail.com", nonce=q["nonce"], email_verified=False
    )
    res = client.get(
        f"/api/auth/google/callback?code={code}&state={q['state']}", follow_redirects=False
    )
    assert "error=google_failed" in res.headers["location"]

    _, q = _google_start(client)
    code = internet.google.login_code(
        sub="g-2", email="x@gmail.com", nonce=q["nonce"], aud="another-app"
    )
    res = client.get(
        f"/api/auth/google/callback?code={code}&state={q['state']}", follow_redirects=False
    )
    assert "error=google_failed" in res.headers["location"]

    _, q = _google_start(client)
    res = client.get(
        f"/api/auth/google/callback?error=access_denied&state={q['state']}", follow_redirects=False
    )
    assert "error=google_cancelled" in res.headers["location"]
    assert client.get("/api/me").status_code == 401


def test_google_sign_in_on_mobile(client, internet):
    _, q = _google_start(client, mobile=True)
    code = internet.google.login_code(sub="g-m", email="m@gmail.com", nonce=q["nonce"])
    res = client.get(
        f"/api/auth/google/callback?code={code}&state={q['state']}", follow_redirects=False
    )
    location = urlsplit(res.headers["location"])
    assert f"{location.scheme}://{location.netloc}" == "defog://auth"
    one_time = parse_qs(location.query)["code"][0]
    tokens = new_client().post("/api/auth/mobile/exchange", json={"token": one_time}).json()
    assert tokens["access_token"] and tokens["refresh_token"]
    assert (
        new_client().post("/api/auth/mobile/exchange", json={"token": one_time}).status_code == 400
    )


# --- Account settings ----------------------------------------------------------------------------------


def test_profile_update_validation(client):
    student = register(client)
    assert student.patch("/api/me", json={"time_zone": "Nowhere/Land"}).status_code == 422
    assert student.patch("/api/me", json={"school": None}).status_code == 422
    assert student.patch("/api/me", json={"accept_terms": False}).status_code == 422
    res = student.patch("/api/me", json={"year": "Graduate", "major": None})
    assert res.json()["year"] == "Graduate" and res.json()["major"] is None


def test_account_deletion_removes_everything_and_revokes_access(client, internet, db):
    student = register(client)
    connect_canvas(student, internet)
    connect_google_calendar(student, internet)
    chat = student.post("/api/chats").json()
    student.post(f"/api/chats/{chat['id']}/messages", json={"content": "hello"})
    assert db.scalar(select(func.count()).select_from(Message)) == 2

    assert student.delete("/api/me", json={"confirm_email": "wrong@school.edu"}).status_code == 422
    assert student.delete("/api/me", json={"confirm_email": "Ann@School.edu"}).status_code == 204
    assert internet.google.revoked, "Google token should be revoked with Google"
    for model in (User, Session, Connection, Message, AuthToken):
        assert db.scalar(select(func.count()).select_from(model)) == 0, model.__name__
    assert client.get("/api/me").status_code == 401


def test_export_contains_data_but_never_tokens(client, internet):
    student = register(client)
    connect_canvas(student, internet, token="secret-pat-token-123456")
    res = student.get("/api/me/export")
    assert res.status_code == 200
    assert "attachment" in res.headers["content-disposition"]
    text = res.text
    assert "secret-pat-token-123456" not in text and "encrypted_tokens" not in text
    data = res.json()
    assert data["profile"]["email"] == "ann@school.edu" and len(data["connections"]) == 1
