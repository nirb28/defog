"""Small pieces: time maths, HTML cleanup, settings, database URLs, migrations."""

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.chat.timeutil import BadTime, fmt, free_slots, parse_when
from app.config import Settings
from app.connections.sync import submission_status
from app.connections.textutil import html_to_text
from app.db import get_engine, normalize_database_url, uses_transaction_pooler
from app.main import app
from app.models import ALL_TABLES
from app.security import TokenCipher, hash_password, verify_password

NY = ZoneInfo("America/New_York")


# --- Time ----------------------------------------------------------------------------------------


def test_parse_when_reads_dates_and_local_times():
    assert parse_when("2026-11-06", NY) == datetime(2026, 11, 6, 5, 0, tzinfo=timezone.utc)
    assert parse_when("2026-11-06", NY, end_of_day=True).astimezone(NY).time() == time.max
    assert parse_when("2026-11-06T17:00", NY) == datetime(2026, 11, 6, 22, 0, tzinfo=timezone.utc)
    assert parse_when("2026-11-06T17:00:00Z", NY) == datetime(
        2026, 11, 6, 17, 0, tzinfo=timezone.utc
    )
    assert parse_when("", NY) is None and parse_when(None, NY) is None
    with pytest.raises(BadTime):
        parse_when("next friday", NY)


def test_fmt_is_portable_and_local():
    assert fmt(datetime(2026, 11, 7, 4, 59, tzinfo=timezone.utc), NY) == "Fri, Nov 6, 11:59 PM"
    assert (
        fmt(datetime(2026, 11, 6, 13, 5, tzinfo=timezone.utc), NY, with_time=False) == "Fri, Nov 6"
    )


def test_free_slots_merges_overlaps_and_respects_waking_hours():
    day = datetime(2026, 11, 10, tzinfo=NY).date()

    def at(h, m=0):  # noqa: ANN001, ANN202
        return datetime.combine(day, time(h, m), tzinfo=NY)

    busy = [(at(9), at(10)), (at(9, 30), at(11)), (at(12), at(12, 20)), (at(23), at(23, 59))]
    slots = free_slots(busy, at(0), at(23, 59), NY, time(8), time(22), 30)
    assert [
        (s.astimezone(NY).strftime("%H:%M"), e.astimezone(NY).strftime("%H:%M")) for s, e in slots
    ] == [
        ("08:00", "09:00"),
        ("11:00", "12:00"),
        ("12:20", "22:00"),
    ]


def test_free_slots_across_dst_change():
    # Nov 1 2026: clocks go back an hour in New York.
    start = datetime(2026, 10, 31, 12, tzinfo=NY)
    end = datetime(2026, 11, 1, 23, tzinfo=NY)
    slots = free_slots([], start, end, NY, time(8), time(22), 60)
    assert len(slots) == 2
    assert slots[1][0].astimezone(NY).hour == 8 and slots[1][1].astimezone(NY).hour == 22


# --- Canvas parsing ----------------------------------------------------------------------------------


def test_html_to_text():
    html = "<h2>Midterm</h2><p>Covers <b>ch. 1&ndash;6</b>.</p><ul><li>Open note</li><li>90 min</li></ul><script>x()</script>"
    assert html_to_text(html) == "Midterm\n\nCovers ch. 1–6.\n\n- Open note\n- 90 min"
    assert html_to_text(None) is None and html_to_text("") is None


@pytest.mark.parametrize(
    "sub,expected",
    [
        (None, "unsubmitted"),
        ({"workflow_state": "unsubmitted"}, "unsubmitted"),
        ({"workflow_state": "unsubmitted", "missing": True}, "missing"),
        ({"workflow_state": "submitted", "submitted_at": "2026-11-01T00:00:00Z"}, "submitted"),
        ({"workflow_state": "pending_review"}, "submitted"),
        ({"workflow_state": "graded", "score": 9}, "graded"),
        ({"workflow_state": "graded", "score": None, "submitted_at": "x"}, "submitted"),
        ({"excused": True, "missing": True}, "excused"),
    ],
)
def test_submission_status(sub, expected):
    assert submission_status(sub) == expected


# --- Settings and database ---------------------------------------------------------------------------


def test_supabase_urls_are_accepted_as_copied():
    url = normalize_database_url(
        "postgresql://postgres.abc:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
    )
    assert url.startswith("postgresql+psycopg://") and url.endswith("?sslmode=require")
    assert normalize_database_url(
        "postgres://u:p@db.abc.supabase.co:5432/postgres?sslmode=verify-full"
    ).endswith("sslmode=verify-full")
    assert "sslmode" not in normalize_database_url("postgresql://defog:defog@localhost:5432/defog")
    assert uses_transaction_pooler("postgresql+psycopg://u:p@x.pooler.supabase.com:6543/postgres")
    assert not uses_transaction_pooler(
        "postgresql+psycopg://u:p@x.pooler.supabase.com:5432/postgres"
    )


def test_production_refuses_weak_settings():
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(
            environment="production",
            secret_key="",
            app_url="https://defog.fit",
            api_url="https://api.defog.fit",
        )
    with pytest.raises(ValueError, match="https"):
        Settings(
            environment="production",
            secret_key="x" * 40,
            token_encryption_key="y" * 44,
            app_url="http://defog.fit",
            api_url="https://api.defog.fit",
        )
    ok = Settings(
        environment="production",
        secret_key="x" * 40,
        token_encryption_key="y" * 44,
        app_url="https://defog.fit",
        api_url="https://api.defog.fit/",
    )
    assert ok.cookie_secure and ok.api_url == "https://api.defog.fit"


def test_canvas_clients_setting_parses_json():
    s = Settings(
        canvas_oauth_clients='{"https://Canvas.UPenn.edu/": {"client_id": "a", "client_secret": "b"}}'
    )
    assert s.canvas_client_for("https://canvas.upenn.edu") == {
        "client_id": "a",
        "client_secret": "b",
    }


def test_development_token_key_is_a_valid_fernet_key():
    cipher = TokenCipher(Settings(token_encryption_key="").effective_token_key)
    assert cipher.decrypt(cipher.encrypt({"a": 1})) == {"a": 1}


def test_password_hashing():
    h = hash_password("hunter2-hunter2")
    assert h.startswith("$argon2id$") and verify_password(h, "hunter2-hunter2")
    assert not verify_password(h, "wrong") and not verify_password(None, "anything")


# --- Supabase lockdown ---------------------------------------------------------------------------------


def test_every_table_has_row_level_security():
    with get_engine().connect() as conn:
        rows = dict(
            conn.execute(
                text(
                    "SELECT relname, relrowsecurity FROM pg_class WHERE relkind = 'r' AND relnamespace = 'public'::regnamespace"
                )
            ).all()
        )
    for table in [*ALL_TABLES, "alembic_version"]:
        assert rows.get(table) is True, (
            f"{table} must have RLS enabled (call lock_down() in its migration)"
        )


def test_data_api_roles_have_no_grants():
    with get_engine().connect() as conn:
        roles = conn.execute(
            text("SELECT count(*) FROM pg_roles WHERE rolname IN ('anon','authenticated')")
        ).scalar()
        if not roles:
            pytest.skip("No Supabase roles in this database")
        grants = conn.execute(
            text(
                "SELECT table_name, grantee FROM information_schema.role_table_grants "
                "WHERE table_schema = 'public' AND grantee IN ('anon','authenticated')"
            )
        ).all()
        seq = conn.execute(
            text(
                "SELECT has_sequence_privilege('anon', pg_get_serial_sequence('public.messages','seq'), 'USAGE')"
            )
        ).scalar()
    assert grants == [] and seq is False


def test_models_match_migrations(database):
    from alembic import command
    from alembic.util.exc import AutogenerateDiffsDetected

    try:
        command.check(database)
    except AutogenerateDiffsDetected as exc:  # pragma: no cover - message shows the diff
        pytest.fail(f"Models changed without a migration: {exc}")


# --- App -----------------------------------------------------------------------------------------------


def test_health():
    res = TestClient(app).get("/api/health")
    assert res.status_code == 200 and res.json() == {"status": "ok", "provider": "mock"}
    assert res.headers["x-content-type-options"] == "nosniff"


def test_cors_allows_the_web_app_with_credentials():
    res = TestClient(app).options(
        "/api/chats",
        headers={
            "Origin": "https://defog.fit",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-csrf-token,content-type",
        },
    )
    assert res.headers.get("access-control-allow-origin") == "https://defog.fit"
    assert res.headers.get("access-control-allow-credentials") == "true"
    other = TestClient(app).options(
        "/api/chats",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert other.headers.get("access-control-allow-origin") is None


def test_missing_api_key_returns_503(client):
    from app.ai import get_llm
    from app.config import get_settings
    from tests.conftest import register

    student = register(client)
    app.dependency_overrides[get_settings] = lambda: Settings(
        llm_provider="anthropic", anthropic_api_key=""
    )
    chat = student.post("/api/chats").json()["id"]
    res = student.post(f"/api/chats/{chat}/messages", json={"content": "hi"})
    assert res.status_code == 503 and "ANTHROPIC_API_KEY" in res.json()["detail"]
    assert get_llm  # dependency is importable from app.ai
