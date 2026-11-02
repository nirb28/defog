"""In-memory stand-ins for Canvas, Google and the breached-password API.

They're served through an httpx MockTransport installed in app.http, so the real code paths
(URL building, pagination, token refresh, error handling) run unchanged.
"""

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx2 as httpx
import jwt

CANVAS_URL = "https://canvas.test.edu"
GOOGLE_CLIENT_ID = "test-client.apps.googleusercontent.com"
BREACHED = {"password123", "iloveyou1"}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeCanvas:
    def __init__(self) -> None:
        self.valid_tokens: set[str] = set()
        self.courses: list[dict] = []
        self.assignments: dict[str, list[dict]] = {}
        self.forbidden_courses: set[str] = set()
        self.announcements: list[dict] = []
        self.events: list[dict] = []
        self.page_size = 2  # small, so pagination is always exercised
        self.codes: dict[str, str] = {}  # oauth code -> user name
        self.refresh_tokens: dict[str, str] = {}  # refresh -> current access
        self.refresh_fails = False
        self.revoked: list[str] = []
        self.calls: list[str] = []

    def add_token(self, token: str) -> str:
        self.valid_tokens.add(token)
        return token

    def _page(self, request: httpx.Request, items: list) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        start = (page - 1) * self.page_size
        chunk = items[start : start + self.page_size]
        headers = {}
        if start + self.page_size < len(items):
            params = dict(request.url.params)
            params["page"] = str(page + 1)
            query = "&".join(f"{k}={v}" for k, v in params.items())
            headers["Link"] = f'<{CANVAS_URL}{request.url.path}?{query}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path == "/login/oauth2/token":
            return self._oauth(request)
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.valid_tokens:
            return httpx.Response(
                401,
                json={"errors": [{"message": "Invalid access token."}]},
                headers={"WWW-Authenticate": 'Bearer realm="canvas-lms"'},
            )
        if path == "/api/v1/users/self":
            return httpx.Response(200, json={"id": 1, "name": "Test Student"})
        if path == "/api/v1/courses":
            return self._page(request, self.courses)
        if path.startswith("/api/v1/courses/") and path.endswith("/assignments"):
            cid = path.split("/")[4]
            if cid in self.forbidden_courses:
                return httpx.Response(401, json={"status": "unauthorized"})
            return self._page(request, self.assignments.get(cid, []))
        if path == "/api/v1/announcements":
            codes = request.url.params.get_list("context_codes[]")
            assert len(codes) <= 10, "Canvas allows at most 10 context codes"
            return self._page(
                request, [a for a in self.announcements if a["context_code"] in codes]
            )
        if path == "/api/v1/calendar_events":
            codes = request.url.params.get_list("context_codes[]")
            assert len(codes) <= 10
            return self._page(request, [e for e in self.events if e["context_code"] in codes])
        return httpx.Response(404, json={"errors": [{"message": "not found"}]})

    def _oauth(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            token = request.headers.get("authorization", "")[7:]
            self.revoked.append(token)
            self.valid_tokens.discard(token)
            return httpx.Response(200, json={})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("client_id") != "canvas-client" or form.get("client_secret") != "canvas-secret":
            return httpx.Response(401, json={"error": "invalid_client"})
        if form["grant_type"] == "authorization_code":
            if form.get("code") not in self.codes:
                return httpx.Response(400, json={"error": "invalid_grant"})
            access, refresh = (
                self.add_token(f"cv-access-{uuid.uuid4().hex}"),
                f"cv-refresh-{uuid.uuid4().hex}",
            )
            self.refresh_tokens[refresh] = access
            return httpx.Response(
                200,
                json={
                    "access_token": access,
                    "token_type": "Bearer",
                    "refresh_token": refresh,
                    "expires_in": 3600,
                    "user": {"id": 1, "name": self.codes.pop(form["code"])},
                },
            )
        if form["grant_type"] == "refresh_token":
            if self.refresh_fails or form.get("refresh_token") not in self.refresh_tokens:
                return httpx.Response(400, json={"error": "invalid_grant"})
            access = self.add_token(f"cv-access-{uuid.uuid4().hex}")
            self.refresh_tokens[form["refresh_token"]] = access
            return httpx.Response(200, json={"access_token": access, "expires_in": 3600})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})


class FakeGoogle:
    """Token endpoint (sign-in and Calendar), revoke, and the Calendar API."""

    def __init__(self) -> None:
        # code -> what the token endpoint returns for it
        self.codes: dict[str, dict] = {}
        self.access_tokens: set[str] = set()
        self.refresh_tokens: dict[str, str] = {}
        self.revoked: list[str] = []
        self.calendars: list[dict] = [
            {"id": "student@gmail.com", "summary": "Student", "primary": True},
            {"id": "club@group.calendar.google.com", "summary": "Robotics Club"},
        ]
        self.events: dict[str, list[dict]] = {
            "student@gmail.com": [],
            "club@group.calendar.google.com": [],
        }
        self.inserted: list[dict] = []
        self.insert_forbidden = False
        self.last_token_request: dict = {}

    # -- helpers for tests --
    def login_code(
        self,
        *,
        sub: str,
        email: str,
        name: str = "Google Student",
        nonce: str,
        email_verified: bool = True,
        aud: str = GOOGLE_CLIENT_ID,
    ) -> str:
        code = f"login-{uuid.uuid4().hex}"
        claims = {
            "iss": "https://accounts.google.com",
            "aud": aud,
            "sub": sub,
            "email": email,
            "email_verified": email_verified,
            "name": name,
            "nonce": nonce,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
        self.codes[code] = {
            "access_token": "g-login-access",
            "id_token": jwt.encode(
                claims, "google-signing-key-not-checked-by-defog-0123456789", algorithm="HS256"
            ),
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "openid email profile",
        }
        return code

    def calendar_code(
        self, *, write: bool = False, refresh: bool = True, scope: str | None = None
    ) -> str:
        code = f"cal-{uuid.uuid4().hex}"
        access = f"g-access-{uuid.uuid4().hex}"
        self.access_tokens.add(access)
        granted = scope or (
            "https://www.googleapis.com/auth/calendar.readonly"
            + (" https://www.googleapis.com/auth/calendar.events" if write else "")
        )
        body = {
            "access_token": access,
            "expires_in": 3599,
            "token_type": "Bearer",
            "scope": granted,
        }
        if refresh:
            token = f"g-refresh-{uuid.uuid4().hex}"
            self.refresh_tokens[token] = access
            body["refresh_token"] = token
        self.codes[code] = body
        return code

    def revoke_all(self) -> None:
        self.access_tokens.clear()
        self.refresh_tokens.clear()

    # -- HTTP --
    def handle(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "oauth2.googleapis.com" and path == "/token":
            return self._token(request)
        if host == "oauth2.googleapis.com" and path == "/revoke":
            token = parse_qs(request.content.decode())["token"][0]
            self.revoked.append(token)
            return httpx.Response(200)
        if host == "www.googleapis.com":
            return self._calendar(request)
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.last_token_request = form
        if (
            form.get("client_id") != GOOGLE_CLIENT_ID
            or form.get("client_secret") != "google-secret"
        ):
            return httpx.Response(401, json={"error": "invalid_client"})
        if form["grant_type"] == "authorization_code":
            if not form.get("code_verifier"):
                return httpx.Response(
                    400, json={"error": "invalid_request", "error_description": "PKCE"}
                )
            body = self.codes.pop(form.get("code", ""), None)
            if body is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json=body)
        if form["grant_type"] == "refresh_token":
            if form.get("refresh_token") not in self.refresh_tokens:
                return httpx.Response(
                    400,
                    json={
                        "error": "invalid_grant",
                        "error_description": "Token has been expired or revoked.",
                    },
                )
            access = f"g-access-{uuid.uuid4().hex}"
            self.access_tokens.add(access)
            self.refresh_tokens[form["refresh_token"]] = access
            return httpx.Response(200, json={"access_token": access, "expires_in": 3599})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _calendar(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if auth[7:] not in self.access_tokens:
            return httpx.Response(401, json={"error": {"code": 401}})
        path = request.url.path
        if path == "/calendar/v3/users/me/calendarList":
            return httpx.Response(200, json={"items": self.calendars})
        prefix = "/calendar/v3/calendars/"
        if path.startswith(prefix) and path.endswith("/events"):
            from urllib.parse import unquote

            cal = unquote(path[len(prefix) : -len("/events")])
            if cal == "primary":
                cal = next(c["id"] for c in self.calendars if c.get("primary"))
            if request.method == "POST":
                if self.insert_forbidden:
                    return httpx.Response(
                        403, json={"error": {"message": "Insufficient Permission"}}
                    )
                body = json.loads(request.content)
                created = {
                    **body,
                    "id": f"ev{len(self.inserted) + 1}",
                    "htmlLink": f"https://calendar.google.com/event?eid=ev{len(self.inserted) + 1}",
                }
                self.inserted.append(created)
                self.events.setdefault(cal, []).append(created)
                return httpx.Response(200, json=created)
            if cal not in self.events:
                return httpx.Response(404, json={"error": {"code": 404}})
            return httpx.Response(200, json={"items": self.events[cal]})
        return httpx.Response(404)


class FakeInternet:
    def __init__(self) -> None:
        self.canvas = FakeCanvas()
        self.google = FakeGoogle()
        self.hibp_down = False
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        host = request.url.host
        if host == "api.pwnedpasswords.com":
            if self.hibp_down:
                raise httpx.ConnectError("unreachable")
            prefix = request.url.path.rsplit("/", 1)[-1]
            lines = []
            for pw in BREACHED:
                digest = hashlib.sha1(pw.encode()).hexdigest().upper()
                if digest.startswith(prefix):
                    lines.append(f"{digest[5:]}:42")
            lines.append("0000000000000000000000000000000000A:0")  # padding entry
            return httpx.Response(200, text="\r\n".join(lines))
        if host == "canvas.test.edu":
            return self.canvas.handle(request)
        if host in ("oauth2.googleapis.com", "www.googleapis.com"):
            return self.google.handle(request)
        raise AssertionError(f"Unexpected outbound request to {request.url}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def sample_canvas(canvas: FakeCanvas, now: datetime) -> None:
    """Three courses with assignments around `now`, announcements and a course event."""
    canvas.courses = [
        {
            "id": 101,
            "name": "Organic Chemistry I",
            "course_code": "CHEM 2410",
            "term": {"name": "Fall 2026"},
            "enrollments": [
                {"type": "student", "computed_current_score": 91.5, "computed_current_grade": "A-"}
            ],
        },
        {
            "id": 202,
            "name": "Molecular Biology",
            "course_code": "BIOL 205",
            "term": {"name": "Fall 2026"},
            "enrollments": [
                {"type": "student", "computed_current_score": 84.0, "computed_current_grade": "B"}
            ],
        },
        {
            "id": 303,
            "name": "Restricted Seminar",
            "course_code": "SEM 1",
            "enrollments": [{"type": "student"}],
        },
        {"id": 404, "access_restricted_by_date": True},
    ]
    canvas.forbidden_courses = {"303"}
    canvas.assignments = {
        "101": [
            {
                "id": 1,
                "name": "Problem Set 5",
                "due_at": iso(now + timedelta(days=2)),
                "points_possible": 20,
                "html_url": f"{CANVAS_URL}/courses/101/assignments/1",
                "description": "<p>Chapters 7&ndash;8. <b>Show your work.</b></p>",
                "submission": {"workflow_state": "unsubmitted", "score": None, "missing": False},
            },
            {
                "id": 2,
                "name": "Lab Report 3",
                "due_at": iso(now + timedelta(days=5)),
                "points_possible": 50,
                "html_url": f"{CANVAS_URL}/courses/101/assignments/2",
                "submission": {
                    "workflow_state": "submitted",
                    "submitted_at": iso(now - timedelta(hours=1)),
                },
            },
            {
                "id": 3,
                "name": "Quiz 4",
                "due_at": iso(now - timedelta(days=3)),
                "points_possible": 10,
                "html_url": f"{CANVAS_URL}/courses/101/assignments/3",
                "submission": {
                    "workflow_state": "graded",
                    "score": 9.0,
                    "grade": "9",
                    "submitted_at": iso(now - timedelta(days=4)),
                },
            },
            {
                "id": 4,
                "name": "Pre-lab Questions",
                "due_at": iso(now - timedelta(days=1)),
                "points_possible": 5,
                "html_url": f"{CANVAS_URL}/courses/101/assignments/4",
                "submission": {"workflow_state": "unsubmitted", "missing": True},
            },
        ],
        "202": [
            {
                "id": 11,
                "name": "Reading Response",
                "due_at": iso(now + timedelta(days=1)),
                "points_possible": 10,
                "html_url": f"{CANVAS_URL}/courses/202/assignments/11",
                "submission": {"workflow_state": "unsubmitted"},
            },
            {
                "id": 12,
                "name": "Final Project",
                "due_at": iso(now + timedelta(days=30)),
                "points_possible": 100,
                "html_url": f"{CANVAS_URL}/courses/202/assignments/12",
                "submission": None,
            },
            {
                "id": 13,
                "name": "Participation",
                "due_at": None,
                "points_possible": 5,
                "html_url": f"{CANVAS_URL}/courses/202/assignments/13",
                "submission": {"workflow_state": "unsubmitted"},
            },
        ],
    }
    canvas.announcements = [
        {
            "id": 900,
            "title": "Midterm logistics",
            "context_code": "course_202",
            "message": "<p>The <strong>midterm</strong> covers chapters 1-6 and is open-note.</p>",
            "posted_at": iso(now - timedelta(days=2)),
            "author": {"display_name": "Prof. Rivera"},
            "html_url": f"{CANVAS_URL}/courses/202/discussion_topics/900",
        },
        {
            "id": 901,
            "title": "Office hours moved",
            "context_code": "course_101",
            "message": "Office hours are Thursday 3-4pm this week.",
            "posted_at": iso(now - timedelta(days=1)),
            "author": {"display_name": "TA Kim"},
            "html_url": f"{CANVAS_URL}/courses/101/discussion_topics/901",
        },
    ]
    canvas.events = [
        {
            "id": 5000,
            "title": "Review session",
            "context_code": "course_202",
            "start_at": iso(now + timedelta(days=1, hours=2)),
            "end_at": iso(now + timedelta(days=1, hours=3)),
            "all_day": False,
            "html_url": f"{CANVAS_URL}/calendar?event_id=5000",
        },
    ]
