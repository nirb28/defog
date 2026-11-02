"""Test setup: a real Postgres database, fake internet, and helpers to make students.

Tests need a Postgres database they're allowed to wipe. By default:
    postgresql://defog:defog@localhost:5432/defog_test
Override with TEST_DATABASE_URL. Tests refuse to run against a Supabase host.
"""

import os
import re
from urllib.parse import parse_qs, urlsplit

# Settings are read at import time, so the test environment must be set first. Environment
# variables win over backend/.env, so a developer's real keys and database are never used.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://defog:defog@localhost:5432/defog_test"
)
if "supabase" in TEST_DATABASE_URL:
    raise RuntimeError("Refusing to run tests against Supabase: they delete all data.")

os.environ.update(
    {
        "ENVIRONMENT": "development",
        "DATABASE_URL": TEST_DATABASE_URL,
        "APP_URL": "http://localhost:5173",
        "API_URL": "http://localhost:5173",
        "SECRET_KEY": "test-secret-key-test-secret-key-test-secret",
        "TOKEN_ENCRYPTION_KEY": "dGVzdC10b2tlbi1lbmNyeXB0aW9uLWtleS0zMmJ5dGU=",
        "BACKGROUND_SYNC_ENABLED": "false",
        "PASSWORD_BREACH_CHECK": "true",
        "EMAIL_BACKEND": "console",
        "GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "google-secret",
        "CANVAS_OAUTH_CLIENTS": '{"https://canvas.test.edu": {"client_id": "canvas-client", "client_secret": "canvas-secret"}}',
        "CANVAS_ALLOWED_HOSTS": "",
        "MOBILE_REDIRECT_URI": "defog://auth",
        "LLM_PROVIDER": "mock",
        "ANTHROPIC_API_KEY": "",
        "OPENAI_API_KEY": "",
        "OPENAI_BASE_URL": "",
        "OPENAI_MODEL": "",
        "MAX_OUTPUT_TOKENS": "1024",
        "MAX_HISTORY_MESSAGES": "20",
        "MAX_MESSAGE_CHARS": "8000",
        "DAILY_TOKEN_LIMIT": "200000",
        "CORS_ORIGINS": "http://localhost:5173,https://defog.fit,https://www.defog.fit",
    }
)

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app import emails, http  # noqa: E402
from app.connections import canvas  # noqa: E402
from app.db import get_engine, normalize_database_url, session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ALL_TABLES  # noqa: E402

from .fakes import FakeInternet  # noqa: E402

REAL_ASSERT_PUBLIC_HOST = canvas.assert_public_host
BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
PASSWORD = "correct-horse-battery"
ZONE = "America/New_York"


@pytest.fixture(scope="session", autouse=True)
def database():
    """Rebuild the schema from the migrations, as Supabase would get it."""
    engine = create_engine(normalize_database_url(TEST_DATABASE_URL))
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        # Recreate Supabase's behaviour: new tables are granted to the Data API roles by default.
        roles = (
            conn.execute(
                text("SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated')")
            )
            .scalars()
            .all()
        )
        for role in roles:
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            conn.execute(
                text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {role}")
            )
            conn.execute(
                text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {role}")
            )
    engine.dispose()

    config = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(BACKEND_DIR, "migrations"))
    config.attributes["database_url"] = TEST_DATABASE_URL
    command.upgrade(config, "head")
    yield config
    get_engine().dispose()


@pytest.fixture(autouse=True)
def clean_tables():
    yield
    tables = ", ".join(f'"{t}"' for t in ALL_TABLES)
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    emails.outbox.clear()
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def internet(monkeypatch):
    fake = FakeInternet()
    monkeypatch.setattr(http, "transport", fake.transport())
    # Test hosts aren't in DNS; the public-address check has its own tests.
    monkeypatch.setattr(canvas, "assert_public_host", lambda base_url: None)
    return fake


@pytest.fixture
def client():
    with TestClient(app, base_url="http://localhost:5173") as c:
        yield c


def new_client() -> TestClient:
    return TestClient(app, base_url="http://localhost:5173")


@pytest.fixture
def db():
    with session_scope() as session:
        yield session


# --- Helpers ---------------------------------------------------------------------------------


def last_email_link(path: str) -> str:
    """The token from the newest emailed link to `path`."""
    for msg in reversed(emails.outbox):
        body = msg.get_content()
        match = re.search(rf"http://localhost:5173{re.escape(path)}\?\S+", body)
        if match:
            return parse_qs(urlsplit(match.group(0)).query)["token"][0]
    raise AssertionError(f"no email with a {path} link")


class Student:
    """A logged-in web client plus the CSRF header it must send."""

    def __init__(self, client: TestClient, email: str, csrf: str, user: dict) -> None:
        self.client = client
        self.email = email
        self.csrf = csrf
        self.user = user

    def _h(self, extra: dict | None = None) -> dict:
        return {"X-CSRF-Token": self.csrf, **(extra or {})}

    def get(self, url: str, **kw):
        return self.client.get(url, **kw)

    def post(self, url: str, **kw):
        return self.client.post(url, headers=self._h(kw.pop("headers", None)), **kw)

    def patch(self, url: str, **kw):
        return self.client.patch(url, headers=self._h(kw.pop("headers", None)), **kw)

    def delete(self, url: str, **kw):
        return self.client.request("DELETE", url, headers=self._h(kw.pop("headers", None)), **kw)


def register(
    client: TestClient, email: str = "ann@school.edu", verify: bool = True, **fields
) -> Student:
    body = {
        "name": "Ann Lee",
        "email": email,
        "password": PASSWORD,
        "school": "University of Pennsylvania",
        "year": "Junior",
        "major": "Biochemistry",
        "time_zone": ZONE,
        "accept_terms": True,
        **fields,
    }
    res = client.post("/api/auth/register", json=body)
    assert res.status_code == 201, res.text
    data = res.json()
    student = Student(client, email, data["csrf_token"], data["user"])
    if verify:
        token = last_email_link("/verify-email")
        assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    return student


@pytest.fixture
def student(client) -> Student:
    return register(client)


def connect_canvas(
    student: Student, internet: FakeInternet, token: str = "pat-token-1234567890"
) -> dict:
    internet.canvas.add_token(token)
    res = student.post(
        "/api/connections/canvas/token", json={"base_url": "canvas.test.edu", "token": token}
    )
    assert res.status_code == 200, res.text
    return res.json()


def connect_google_calendar(student: Student, internet: FakeInternet, write: bool = False) -> None:
    res = student.get(
        f"/api/connections/google-calendar/start?write={str(write).lower()}", follow_redirects=False
    )
    assert res.status_code == 303, res.text
    state = parse_qs(urlsplit(res.headers["location"]).query)["state"][0]
    code = internet.google.calendar_code(write=write)
    res = student.get(
        f"/api/connections/google-calendar/callback?code={code}&state={state}",
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "connected=google_calendar" in res.headers["location"], res.headers["location"]


def sse_events(text_body: str) -> list[tuple[str, dict]]:
    import json

    events = []
    for block in text_body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events
