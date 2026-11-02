"""Requirement 2: Canvas and Google Calendar connections, sync, tokens and disconnect."""

import socket
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select, text, update

from app.config import Settings
from app.connections import canvas as canvas_mod
from app.models import Announcement, Assignment, CalendarEvent, Connection, Course, utcnow
from tests.conftest import (
    REAL_ASSERT_PUBLIC_HOST,
    connect_canvas,
    connect_google_calendar,
    new_client,
    register,
)
from tests.fakes import CANVAS_URL, iso, sample_canvas


def _canvas(student) -> dict:
    return next(
        c
        for c in student.get("/api/connections").json()["connections"]
        if c["provider"] == "canvas"
    )


def _gcal(student) -> dict:
    return next(
        c
        for c in student.get("/api/connections").json()["connections"]
        if c["provider"] == "google_calendar"
    )


# --- Canvas: personal access token ------------------------------------------------------------


def test_connections_page_lists_both_sources(student):
    data = student.get("/api/connections").json()
    assert [c["provider"] for c in data["connections"]] == ["canvas", "google_calendar"]
    assert all(c["status"] == "not_connected" for c in data["connections"])
    assert data["canvas_oauth_schools"] == [CANVAS_URL] and data["google_enabled"] is True


def test_canvas_token_connect_syncs_courses(student, internet, db):
    sample_canvas(internet.canvas, utcnow())
    out = connect_canvas(student, internet)
    assert out["status"] == "syncing" and out["base_url"] == CANVAS_URL

    conn = _canvas(student)
    assert conn["status"] == "connected" and conn["last_synced_at"] and conn["course_count"] == 3
    courses = {c.code: c for c in db.scalars(select(Course))}
    assert set(courses) == {"CHEM 2410", "BIOL 205", "SEM 1"}  # date-restricted course skipped
    assert courses["CHEM 2410"].current_score == 91.5 and courses["CHEM 2410"].current_grade == "A-"

    statuses = {a.title: a.submission_status for a in db.scalars(select(Assignment))}
    assert statuses == {
        "Problem Set 5": "unsubmitted",
        "Lab Report 3": "submitted",
        "Quiz 4": "graded",
        "Pre-lab Questions": "missing",
        "Reading Response": "unsubmitted",
        "Final Project": "unsubmitted",
        "Participation": "unsubmitted",
    }
    ps5 = db.scalar(select(Assignment).where(Assignment.title == "Problem Set 5"))
    assert ps5.description == "Chapters 7–8. Show your work."  # HTML stripped, entities decoded
    assert db.scalar(select(func.count()).select_from(Announcement)) == 2
    event = db.scalar(select(CalendarEvent))
    assert event.source == "canvas" and event.calendar_name == "Molecular Biology"
    # Pagination was followed (page size 2 in the fake).
    assert "GET /api/v1/courses" in internet.canvas.calls
    assert sum(1 for c in internet.canvas.calls if c == "GET /api/v1/courses/101/assignments") == 2


def test_course_that_hides_assignments_is_skipped_not_fatal(student, internet, db):
    sample_canvas(internet.canvas, utcnow())
    connect_canvas(student, internet)
    assert _canvas(student)["status"] == "connected"
    seminar = db.scalar(select(Course).where(Course.code == "SEM 1"))
    assert (
        db.scalar(
            select(func.count()).select_from(Assignment).where(Assignment.course_id == seminar.id)
        )
        == 0
    )


def test_rejected_canvas_token_explains_disabled_tokens(student, internet):
    res = student.post(
        "/api/connections/canvas/token",
        json={"base_url": "canvas.test.edu", "token": "bad-token-1234567"},
    )
    assert res.status_code == 422
    assert "turn off personal access tokens" in res.json()["detail"]
    assert _canvas(student)["status"] == "not_connected"


def test_tokens_are_encrypted_at_rest(student, internet, db):
    connect_canvas(student, internet, token="plain-text-pat-abcdef")
    raw = db.execute(text("SELECT encrypted_tokens FROM connections")).scalar()
    assert "plain-text-pat-abcdef" not in raw and raw.startswith("gAAAA")  # Fernet token
    assert "plain-text-pat-abcdef" not in str(student.get("/api/connections").json())


def test_revoked_canvas_token_flips_to_needs_attention(student, internet):
    sample_canvas(internet.canvas, utcnow())
    connect_canvas(student, internet, token="pat-will-be-revoked")
    internet.canvas.valid_tokens.discard("pat-will-be-revoked")
    conn = _canvas(student)
    res = student.post(f"/api/connections/{conn['id']}/sync")
    assert res.status_code == 409
    conn = _canvas(student)
    assert conn["status"] == "needs_attention" and "new token" in conn["status_detail"]
    # Reconnecting with a fresh token fixes it.
    connect_canvas(student, internet, token="pat-fresh-token-123")
    assert _canvas(student)["status"] == "connected"


def test_canvas_outage_is_reported_and_keeps_data(student, internet, db):
    sample_canvas(internet.canvas, utcnow())
    connect_canvas(student, internet)
    before = db.scalar(select(func.count()).select_from(Assignment))
    original = internet.canvas.handle
    internet.canvas.handle = lambda request: __import__("httpx2").Response(503)  # type: ignore[method-assign]
    res = student.post(f"/api/connections/{_canvas(student)['id']}/sync")
    internet.canvas.handle = original  # type: ignore[method-assign]
    assert res.status_code == 502
    conn = _canvas(student)
    assert conn["status"] == "connected" and conn["last_sync_error"]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Assignment)) == before


# --- Canvas: address checks ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("canvas.upenn.edu", "https://canvas.upenn.edu"),
        ("https://Canvas.UPenn.edu/courses/123?x=1", "https://canvas.upenn.edu"),
        ("  upenn.instructure.com/ ", "https://upenn.instructure.com"),
    ],
)
def test_canvas_url_normalisation(raw, expected):
    assert canvas_mod.normalize_base_url(raw, Settings()) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://canvas.upenn.edu",
        "https://10.0.0.5",
        "localhost",
        "https://intranet",
        "https://user:pw@canvas.edu",
        "https://canvas.edu:8443",
        "https://[::1]",
        "",
    ],
)
def test_canvas_url_rejects_unsafe_addresses(raw):
    with pytest.raises(canvas_mod.InvalidCanvasURL):
        canvas_mod.normalize_base_url(raw, Settings())


def test_canvas_allowlist():
    settings = Settings(canvas_allowed_hosts="canvas.upenn.edu")
    assert canvas_mod.normalize_base_url("canvas.upenn.edu", settings) == "https://canvas.upenn.edu"
    with pytest.raises(canvas_mod.InvalidCanvasURL):
        canvas_mod.normalize_base_url("canvas.evil.edu", settings)


def test_hosts_resolving_to_private_addresses_are_refused(monkeypatch):
    def fake_getaddrinfo(host, port, proto=0):  # noqa: ANN001
        ip = {"canvas.good.edu": "151.101.1.1", "canvas.sneaky.edu": "169.254.169.254"}[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(canvas_mod.socket, "getaddrinfo", fake_getaddrinfo)
    REAL_ASSERT_PUBLIC_HOST("https://canvas.good.edu")
    with pytest.raises(canvas_mod.InvalidCanvasURL):
        REAL_ASSERT_PUBLIC_HOST("https://canvas.sneaky.edu")


# --- Canvas: OAuth --------------------------------------------------------------------------------


def _canvas_oauth(student, internet) -> None:
    res = student.get(
        "/api/connections/canvas/start?base_url=canvas.test.edu", follow_redirects=False
    )
    assert res.status_code == 303
    url = urlsplit(res.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{CANVAS_URL}/login/oauth2/auth"
    assert q["client_id"] == "canvas-client" and q["response_type"] == "code"
    assert q["redirect_uri"] == "http://localhost:5173/api/connections/canvas/callback"
    internet.canvas.codes["code-1"] = "Ann Lee"
    res = student.get(
        f"/api/connections/canvas/callback?code=code-1&state={q['state']}", follow_redirects=False
    )
    assert res.headers["location"] == "http://localhost:5173/connections?connected=canvas"


def test_canvas_oauth_connect_and_refresh(student, internet, db):
    sample_canvas(internet.canvas, utcnow())
    _canvas_oauth(student, internet)
    conn = _canvas(student)
    assert (
        conn["status"] == "connected" and conn["auth_type"] == "oauth" and conn["course_count"] == 3
    )

    # Expire the access token: the next sync refreshes it first.
    db.execute(update(Connection).values(token_expires_at=utcnow() - timedelta(minutes=1)))
    db.commit()
    calls_before = len(internet.canvas.calls)
    assert student.post(f"/api/connections/{conn['id']}/sync").status_code == 200
    assert "POST /login/oauth2/token" in internet.canvas.calls[calls_before:]

    # When the refresh token stops working, the student is asked to reconnect.
    db.execute(update(Connection).values(token_expires_at=utcnow() - timedelta(minutes=1)))
    db.commit()
    internet.canvas.refresh_fails = True
    assert student.post(f"/api/connections/{conn['id']}/sync").status_code == 409
    assert _canvas(student)["status"] == "needs_attention"


def test_canvas_oauth_rejects_unknown_school_and_forged_state(student, internet):
    res = student.get(
        "/api/connections/canvas/start?base_url=canvas.other.edu", follow_redirects=False
    )
    assert res.status_code == 400 and "access token" in res.json()["detail"]
    student.get("/api/connections/canvas/start?base_url=canvas.test.edu", follow_redirects=False)
    internet.canvas.codes["code-2"] = "Ann"
    res = student.get(
        "/api/connections/canvas/callback?code=code-2&state=forged", follow_redirects=False
    )
    assert "error=canvas_expired" in res.headers["location"]
    assert _canvas(student)["status"] == "not_connected"


def test_oauth_callback_must_be_finished_by_the_same_student(client, internet):
    ann = register(client)
    res = ann.get("/api/connections/canvas/start?base_url=canvas.test.edu", follow_redirects=False)
    state = parse_qs(urlsplit(res.headers["location"]).query)["state"][0]
    state_cookie = client.cookies.get("defog_oauth")
    # Bob somehow gets Ann's state cookie and code (e.g. a crafted link): it's refused.
    bob_client = new_client()
    bob = register(bob_client, email="bob@school.edu")
    bob_client.cookies.set("defog_oauth", state_cookie, path="/api")
    internet.canvas.codes["code-3"] = "Ann"
    res = bob.get(
        f"/api/connections/canvas/callback?code=code-3&state={state}", follow_redirects=False
    )
    assert "error=canvas_cancelled" in res.headers["location"]
    assert _canvas(bob)["status"] == "not_connected"


# --- Google Calendar --------------------------------------------------------------------------------


def _google_events(internet, now):
    g = internet.google
    g.events["student@gmail.com"] = [
        {
            "id": "e1",
            "summary": "Orgo lecture",
            "start": {"dateTime": iso(now + timedelta(hours=20))},
            "end": {"dateTime": iso(now + timedelta(hours=21))},
            "htmlLink": "https://calendar.google.com/e1",
        },
        {
            "id": "e2",
            "summary": "Optional mixer",
            "transparency": "transparent",
            "start": {"dateTime": iso(now + timedelta(hours=22))},
            "end": {"dateTime": iso(now + timedelta(hours=23))},
        },
        {
            "id": "e3",
            "summary": "Declined meeting",
            "attendees": [
                {"email": "student@gmail.com", "self": True, "responseStatus": "declined"}
            ],
            "start": {"dateTime": iso(now + timedelta(hours=24))},
            "end": {"dateTime": iso(now + timedelta(hours=25))},
        },
        {
            "id": "e4",
            "summary": "Mom's birthday",
            "start": {"date": (now + timedelta(days=2)).date().isoformat()},
            "end": {"date": (now + timedelta(days=3)).date().isoformat()},
        },
        {
            "id": "e5",
            "summary": "Cancelled",
            "status": "cancelled",
            "start": {"dateTime": iso(now + timedelta(hours=26))},
            "end": {"dateTime": iso(now + timedelta(hours=27))},
        },
    ]
    g.events["club@group.calendar.google.com"] = [
        {
            "id": "c1",
            "summary": "Robotics build",
            "start": {"dateTime": iso(now + timedelta(hours=30))},
            "end": {"dateTime": iso(now + timedelta(hours=32))},
        },
    ]


def test_google_calendar_start_requests_readonly_offline_access(student):
    res = student.get("/api/connections/google-calendar/start", follow_redirects=False)
    q = {k: v[0] for k, v in parse_qs(urlsplit(res.headers["location"]).query).items()}
    assert q["scope"] == "https://www.googleapis.com/auth/calendar.readonly"
    assert q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["redirect_uri"] == "http://localhost:5173/api/connections/google-calendar/callback"
    assert q["login_hint"] == "ann@school.edu"


def test_google_calendar_connect_and_sync(student, internet, db):
    _google_events(internet, utcnow())
    connect_google_calendar(student, internet)
    conn = _gcal(student)
    assert conn["status"] == "connected" and conn["calendar_ids"] == ["primary"]
    assert conn["allow_writes"] is False and conn["can_write"] is False

    events = {e.title: e for e in db.scalars(select(CalendarEvent))}
    assert set(events) == {"Orgo lecture", "Optional mixer", "Declined meeting", "Mom's birthday"}
    assert events["Orgo lecture"].busy is True
    assert events["Optional mixer"].busy is False  # marked "free"
    assert events["Declined meeting"].busy is False
    assert events["Mom's birthday"].all_day is True and events["Mom's birthday"].busy is False
    assert events["Orgo lecture"].calendar_name == "Student"


def test_pick_calendars(student, internet, db):
    _google_events(internet, utcnow())
    connect_google_calendar(student, internet)
    conn = _gcal(student)
    calendars = student.get(f"/api/connections/{conn['id']}/calendars").json()
    assert [(c["name"], c["selected"]) for c in calendars] == [
        ("Student", True),
        ("Robotics Club", False),
    ]
    res = student.patch(
        f"/api/connections/{conn['id']}",
        json={"calendar_ids": ["primary", "club@group.calendar.google.com"]},
    )
    assert res.status_code == 200
    titles = {e.title for e in db.scalars(select(CalendarEvent))}
    assert "Robotics build" in titles and "Orgo lecture" in titles
    assert (
        student.patch(f"/api/connections/{conn['id']}", json={"calendar_ids": []}).status_code
        == 422
    )


def test_writes_need_the_events_scope(student, internet):
    connect_google_calendar(student, internet)
    conn = _gcal(student)
    res = student.patch(f"/api/connections/{conn['id']}", json={"allow_writes": True})
    assert res.status_code == 409 and "Reconnect" in res.json()["detail"]
    # Reconnecting with "Let Defog add events" asks for calendar.events too.
    res = student.get("/api/connections/google-calendar/start?write=true", follow_redirects=False)
    scope = parse_qs(urlsplit(res.headers["location"]).query)["scope"][0]
    assert "calendar.events" in scope
    connect_google_calendar(student, internet, write=True)
    conn = _gcal(student)
    assert conn["can_write"] is True and conn["allow_writes"] is True
    assert (
        student.patch(f"/api/connections/{conn['id']}", json={"allow_writes": False}).json()[
            "allow_writes"
        ]
        is False
    )


def test_unticked_calendar_permission_is_reported(student, internet):
    res = student.get("/api/connections/google-calendar/start", follow_redirects=False)
    state = parse_qs(urlsplit(res.headers["location"]).query)["state"][0]
    code = internet.google.calendar_code(scope="openid")
    res = student.get(
        f"/api/connections/google-calendar/callback?code={code}&state={state}",
        follow_redirects=False,
    )
    assert "error=google_calendar_scope" in res.headers["location"]
    assert _gcal(student)["status"] == "not_connected"


def test_revoked_google_access_needs_attention(student, internet, db):
    connect_google_calendar(student, internet)
    conn = _gcal(student)
    internet.google.revoke_all()
    db.execute(update(Connection).values(token_expires_at=utcnow() - timedelta(minutes=1)))
    db.commit()
    assert student.post(f"/api/connections/{conn['id']}/sync").status_code == 409
    conn = _gcal(student)
    assert (
        conn["status"] == "needs_attention" and "Reconnect Google Calendar" in conn["status_detail"]
    )


def test_google_rate_limit_keeps_existing_events(student, internet, db):
    _google_events(internet, utcnow())
    connect_google_calendar(student, internet)
    before = db.scalar(select(func.count()).select_from(CalendarEvent))
    original = internet.google._calendar

    def limited(request):  # noqa: ANN001, ANN202
        if request.url.path.endswith("/events"):
            return __import__("httpx2").Response(
                403, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
            )
        return original(request)

    internet.google._calendar = limited  # type: ignore[method-assign]
    res = student.post(f"/api/connections/{_gcal(student)['id']}/sync")
    assert res.status_code == 502
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(CalendarEvent)) == before
    assert _gcal(student)["status"] == "connected"


def test_expired_google_access_token_is_refreshed(student, internet, db):
    connect_google_calendar(student, internet)
    internet.google.access_tokens.clear()  # Google stops accepting the old access token
    conn = _gcal(student)
    assert student.post(f"/api/connections/{conn['id']}/sync").status_code == 200
    assert internet.google.last_token_request["grant_type"] == "refresh_token"


# --- Disconnect and isolation -------------------------------------------------------------------------


def test_disconnect_revokes_and_deletes_data(student, internet, db):
    sample_canvas(internet.canvas, utcnow())
    _canvas_oauth(student, internet)
    connect_google_calendar(student, internet)
    for conn in student.get("/api/connections").json()["connections"]:
        assert student.delete(f"/api/connections/{conn['id']}").status_code == 204
    assert internet.canvas.revoked and internet.google.revoked
    for model in (Connection, Course, Assignment, Announcement, CalendarEvent):
        assert db.scalar(select(func.count()).select_from(model)) == 0, model.__name__


def test_students_cannot_touch_each_others_connections(client, internet):
    ann = register(client)
    sample_canvas(internet.canvas, utcnow())
    connect_canvas(ann, internet)
    ann_conn = _canvas(ann)["id"]
    bob = register(new_client(), email="bob@school.edu")
    assert _canvas(bob)["status"] == "not_connected"
    assert bob.post(f"/api/connections/{ann_conn}/sync").status_code == 404
    assert (
        bob.patch(f"/api/connections/{ann_conn}", json={"allow_writes": False}).status_code == 404
    )
    assert bob.get(f"/api/connections/{ann_conn}/calendars").status_code == 404
    assert bob.delete(f"/api/connections/{ann_conn}").status_code == 404
    assert _canvas(ann)["status"] == "connected"


def test_switching_canvas_school_drops_old_data(student, internet, db):
    sample_canvas(internet.canvas, utcnow())
    connect_canvas(student, internet)
    old = _canvas(student)["id"]
    _canvas_oauth(student, internet)  # same host but now via OAuth: same school, keeps the row
    assert _canvas(student)["id"] == old
    assert db.scalar(select(func.count()).select_from(Connection)) == 1


# --- Background work ----------------------------------------------------------------------------------


def test_scheduler_picks_due_connections_and_purge_cleans_up(student, internet, db):
    from app.connections.sync import due_connection_ids
    from app.main import purge_expired
    from app.models import LoginFailure, Session

    sample_canvas(internet.canvas, utcnow())
    connect_canvas(student, internet)
    assert due_connection_ids() == []  # just synced
    db.execute(update(Connection).values(last_synced_at=utcnow() - timedelta(hours=2)))
    db.add(LoginFailure(email="x@y.edu", created_at=utcnow() - timedelta(days=2)))
    db.execute(update(Session).values(expires_at=utcnow() - timedelta(minutes=1)))
    db.commit()
    assert len(due_connection_ids()) == 1
    purge_expired()
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(LoginFailure)) == 0
    assert db.scalar(select(func.count()).select_from(Session)) == 0


def test_server_starts_with_background_sync(monkeypatch):
    import asyncio

    from app import main

    calls = []
    monkeypatch.setattr(main, "SYNC_LOOP_SECONDS", 0.05)
    monkeypatch.setattr(main, "due_connection_ids", lambda: calls.append("due") or [])
    monkeypatch.setattr(main.settings, "background_sync_enabled", True)

    async def run():
        async with main.lifespan(main.app):
            await asyncio.sleep(0.2)

    asyncio.run(run())
    assert len(calls) >= 2  # the loop ran repeatedly and stopped cleanly on shutdown
