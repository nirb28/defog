"""Requirement 3: the chatbot answers from the student's own data, via tools."""

import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from app.ai import get_llm
from app.llm import LLMClient, LLMError, TextDelta, ToolCall, TurnEnd
from app.main import app
from app.models import CalendarEvent, Connection, LLMUsage, Message, PendingAction, utcnow
from tests.conftest import (
    ZONE,
    connect_canvas,
    connect_google_calendar,
    new_client,
    register,
    sse_events,
)
from tests.fakes import iso, sample_canvas

NY = ZoneInfo(ZONE)


class ScriptedLLM(LLMClient):
    """Calls the given tools on the first round, then answers. Records what it saw."""

    provider = "fake"
    model = "fake-1"

    def __init__(self, calls: list[tuple[str, dict]], answer: str = "Here you go.") -> None:
        self.calls = calls
        self.answer = answer
        self.rounds: list[list[dict]] = []
        self.systems: list[str] = []
        self.tool_names: list[list[str]] = []

    async def stream_turn(self, system, messages, tools):  # noqa: ANN001, ANN201
        self.rounds.append([dict(m) for m in messages])
        self.systems.append(system)
        self.tool_names.append([t.name for t in tools])
        first_round = not any(m["role"] == "tool" for m in messages)
        if self.calls and first_round:
            yield TextDelta("Let me check. ")
            yield TurnEnd(
                text="Let me check. ",
                tool_calls=[
                    ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(self.calls)
                ],
                input_tokens=100,
                output_tokens=10,
            )
            return
        for word in self.answer.split(" "):
            yield TextDelta(word + " ")
        yield TurnEnd(text=self.answer, input_tokens=200, output_tokens=20)

    def results(self) -> dict[str, dict]:
        """Tool results the model received, by tool name."""
        last = self.rounds[-1]
        return {m["name"]: json.loads(m["content"]) for m in last if m["role"] == "tool"}


def use_llm(llm: LLMClient) -> None:
    app.dependency_overrides[get_llm] = lambda: llm


def ask(student, text: str, chat_id: str | None = None):
    chat_id = chat_id or student.post("/api/chats").json()["id"]
    res = student.post(f"/api/chats/{chat_id}/messages", json={"content": text})
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/event-stream")
    return chat_id, sse_events(res.text)


def ready(client, internet, google: bool = False, write: bool = False):
    student = register(client)
    sample_canvas(internet.canvas, utcnow())
    connect_canvas(student, internet)
    if google:
        now = utcnow()
        internet.google.events["student@gmail.com"] = [
            {
                "id": "g1",
                "summary": "Lecture",
                "start": {"dateTime": iso(now + timedelta(hours=26))},
                "end": {"dateTime": iso(now + timedelta(hours=27))},
                "htmlLink": "https://calendar.google.com/g1",
            },
        ]
        connect_google_calendar(student, internet, write=write)
    return student


# --- Basics -------------------------------------------------------------------------------------


def test_whats_due_this_week_with_mock_provider(client, internet):
    """The default mock provider runs the real tool and lists the results."""
    student = ready(client, internet)
    chat_id, events = ask(student, "What's due this week?")
    kinds = [k for k, _ in events]
    assert kinds[0] == "tool" and events[0][1]["name"] == "get_assignments"
    assert "delta" in kinds and kinds[-1] == "done"
    answer = "".join(d["text"] for k, d in events if k == "delta")
    assert "Reading Response" in answer and "Problem Set 5" in answer
    assert "Lab Report 3" not in answer  # already submitted
    assert "Final Project" not in answer  # due in 30 days

    detail = student.get(f"/api/chats/{chat_id}").json()
    assert detail["title"] == "What's due this week?"
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["sources"] == ["Canvas assignments"]


def test_assignments_tool_returns_due_dates_in_student_time_zone(client, internet):
    student = ready(client, internet)
    llm = ScriptedLLM([("get_assignments", {})])
    use_llm(llm)
    _, events = ask(student, "What's due?")
    result = llm.results()["get_assignments"]
    titles = [i["title"] for i in result["items"]]
    assert titles == ["Reading Response", "Problem Set 5"]  # soonest first, unsubmitted only
    due = datetime.fromisoformat(result["items"][0]["due_iso"])
    expected = (utcnow() + timedelta(days=1)).astimezone(NY)
    assert due.utcoffset() == expected.utcoffset()  # given in America/New_York
    assert result["items"][0]["due"].startswith(f"{expected:%a}, {expected:%b} {expected.day}, ")
    assert result["items"][0]["url"].endswith("/courses/202/assignments/11")
    # Streaming: text arrives in several deltas, with a gap between the tool round and the answer.
    deltas = [d["text"] for k, d in events if k == "delta"]
    assert len(deltas) > 3 and "\n\n" in deltas


def test_system_prompt_has_time_zone_sources_and_injection_guard(client, internet):
    student = ready(client, internet)
    llm = ScriptedLLM([])
    use_llm(llm)
    ask(student, "hi")
    system = llm.systems[0]
    assert "America/New_York" in system
    assert "Canvas: connected" in system and "Google Calendar: not connected" in system
    assert "never instructions" in system
    assert llm.tool_names[0] == [
        "get_assignments",
        "get_grades",
        "get_announcements",
        "search_course_content",
        "get_events",
        "find_free_time",
        "create_event",
    ]


def test_course_filter_grades_and_missing(client, internet):
    student = ready(client, internet)
    llm = ScriptedLLM(
        [
            ("get_grades", {"course": "chem2410"}),
            ("get_assignments", {"status": "missing"}),
            ("get_announcements", {"course": "Orgo"}),
        ]
    )
    use_llm(llm)
    ask(student, "How am I doing in Orgo?")
    r = llm.results()
    grades = r["get_grades"]["courses"]
    assert len(grades) == 1 and grades[0]["current_score"] == 91.5 and grades[0]["grade"] == "A-"
    assert grades[0]["recent_scores"][0]["title"] == "Quiz 4"
    assert [i["title"] for i in r["get_assignments"]["items"]] == ["Pre-lab Questions"]
    # "Orgo" matches no name or code: the model is told the real course names so it can retry.
    assert "Organic Chemistry I" in r["get_announcements"]["error"]


def test_search_what_the_professor_said(client, internet):
    student = ready(client, internet)
    llm = ScriptedLLM([("search_course_content", {"query": "midterm"})])
    use_llm(llm)
    ask(student, "What did the professor say about the midterm?")
    items = llm.results()["search_course_content"]["items"]
    assert items[0]["title"] == "Midterm logistics" and items[0]["course"].startswith("BIOL 205")
    assert "«midterm»" in items[0]["excerpt"] and "open-note" in items[0]["excerpt"]


def test_not_connected_points_to_connections_page(client, internet):
    student = register(client)
    llm = ScriptedLLM([("get_assignments", {}), ("find_free_time", {})])
    use_llm(llm)
    ask(student, "What's due and when am I free?")
    r = llm.results()
    assert "Canvas isn't connected" in r["get_assignments"]["error"]
    assert "/connections" in r["get_assignments"]["error"]
    assert "Google Calendar isn't connected" in r["find_free_time"]["error"]


# --- Calendar ---------------------------------------------------------------------------------------


def test_free_time_respects_google_and_canvas_events(client, internet, db):
    student = ready(client, internet, google=True)
    tomorrow = (utcnow().astimezone(NY) + timedelta(days=1)).date()
    db.execute(update(CalendarEvent).values(busy=False))  # start from a clean slate...
    db.add_all(
        [
            CalendarEvent(
                user_id=uuid.UUID(student.user["id"]),
                connection_id=_conn_id(db, "google_calendar"),
                source="google_calendar",
                external_id="x1",
                calendar_id="primary",
                title="Class",
                start_at=datetime.combine(
                    tomorrow, datetime.min.time().replace(hour=13), tzinfo=NY
                ),
                end_at=datetime.combine(tomorrow, datetime.min.time().replace(hour=15), tzinfo=NY),
                busy=True,
            ),
            CalendarEvent(
                user_id=uuid.UUID(student.user["id"]),
                connection_id=_conn_id(db, "canvas"),
                source="canvas",
                external_id="x2",
                calendar_id="course_202",
                title="Review",
                start_at=datetime.combine(
                    tomorrow, datetime.min.time().replace(hour=14, minute=30), tzinfo=NY
                ),
                end_at=datetime.combine(tomorrow, datetime.min.time().replace(hour=16), tzinfo=NY),
                busy=True,
            ),
        ]
    )
    db.commit()
    llm = ScriptedLLM(
        [
            (
                "find_free_time",
                {"from": f"{tomorrow}T12:00", "to": f"{tomorrow}T18:00", "min_minutes": 45},
            )
        ]
    )
    use_llm(llm)
    ask(student, "When am I free tomorrow afternoon?")
    slots = llm.results()["find_free_time"]["slots"]
    assert [(s["start_iso"][11:16], s["end_iso"][11:16], s["minutes"]) for s in slots] == [
        ("12:00", "13:00", 60),
        ("16:00", "18:00", 120),
    ]


def _conn_id(db, provider):
    return db.scalar(select(Connection.id).where(Connection.provider == provider))


def test_events_tool_lists_both_sources(client, internet):
    student = ready(client, internet, google=True)
    llm = ScriptedLLM([("get_events", {})])
    use_llm(llm)
    ask(student, "What's on my calendar?")
    items = llm.results()["get_events"]["items"]
    assert {(i["title"], i["source"]) for i in items} >= {
        ("Lecture", "Google Calendar"),
        ("Review session", "Canvas"),
    }


def test_create_event_only_after_confirmation(client, internet, db):
    student = ready(client, internet, google=True, write=True)
    llm = ScriptedLLM(
        [
            (
                "create_event",
                {"title": "Lab report due", "start": "2026-11-14T23:00", "end": "2026-11-14T23:59"},
            )
        ],
        answer="I've prepared that event; confirm it to add it.",
    )
    use_llm(llm)
    chat_id, events = ask(student, "Add my lab report deadline to my calendar")
    actions = [d for k, d in events if k == "action"]
    assert len(actions) == 1 and actions[0]["status"] == "pending"
    assert actions[0]["summary"] == "Add “Lab report due” on Sat, Nov 14, 11:00 PM–11:59 PM?"
    assert internet.google.inserted == []  # nothing written yet
    done = next(d for k, d in events if k == "done")
    assert done["message"]["actions"][0]["id"] == actions[0]["id"]

    res = student.post(f"/api/chats/{chat_id}/actions/{actions[0]['id']}/confirm")
    assert res.status_code == 200, res.text
    assert res.json()["action"]["status"] == "done"
    inserted = internet.google.inserted[0]
    assert inserted["summary"] == "Lab report due"
    assert inserted["start"] == {"dateTime": "2026-11-14T23:00:00-05:00", "timeZone": ZONE}
    assert "Added **Lab report due**" in res.json()["message"]["content"]
    # Shows up in Defog's calendar data right away, and a second click does nothing.
    assert db.scalar(select(CalendarEvent).where(CalendarEvent.external_id == "ev1")) is not None
    assert (
        student.post(f"/api/chats/{chat_id}/actions/{actions[0]['id']}/confirm").status_code == 409
    )
    assert len(internet.google.inserted) == 1

    # Reloading the chat shows the action with its final state.
    messages = student.get(f"/api/chats/{chat_id}").json()["messages"]
    assert messages[1]["actions"][0]["status"] == "done" and messages[-1]["role"] == "assistant"


def test_create_event_refused_without_write_permission(client, internet):
    student = ready(client, internet, google=True, write=False)
    llm = ScriptedLLM([("create_event", {"title": "Study", "start": "2026-11-10T10:00"})])
    use_llm(llm)
    _, events = ask(student, "Block study time")
    assert not [d for k, d in events if k == "action"]
    assert "Let Defog add events" in llm.results()["create_event"]["error"]


def test_calendar_rate_limit_is_not_reported_as_missing_permission(client, internet):
    student = ready(client, internet, google=True, write=True)
    use_llm(ScriptedLLM([("create_event", {"title": "Study", "start": "2026-11-10T10:00"})]))
    chat_id, events = ask(student, "Block study time")
    action = next(d for k, d in events if k == "action")
    original = internet.google._calendar
    internet.google._calendar = lambda request: __import__("httpx2").Response(  # type: ignore[method-assign]
        403,
        json={
            "error": {"errors": [{"reason": "rateLimitExceeded"}], "message": "Rate Limit Exceeded"}
        },
    )
    res = student.post(f"/api/chats/{chat_id}/actions/{action['id']}/confirm")
    internet.google._calendar = original  # type: ignore[method-assign]
    assert res.status_code == 502 and "didn't respond" in res.json()["detail"]
    assert student.post(f"/api/chats/{chat_id}/actions/{action['id']}/confirm").status_code == 200


def test_cancel_action_and_expiry(client, internet, db):
    student = ready(client, internet, google=True, write=True)
    llm = ScriptedLLM(
        [
            ("create_event", {"title": "Study", "start": "2026-11-10T10:00"}),
            ("create_event", {"title": "Gym", "start": "2026-11-10T18:00"}),
        ]
    )
    use_llm(llm)
    chat_id, events = ask(student, "Plan my day")
    first, second = [d for k, d in events if k == "action"]
    assert (
        student.post(f"/api/chats/{chat_id}/actions/{first['id']}/cancel").json()["action"][
            "status"
        ]
        == "cancelled"
    )
    assert student.post(f"/api/chats/{chat_id}/actions/{first['id']}/confirm").status_code == 409
    db.execute(update(PendingAction).values(expires_at=utcnow() - timedelta(minutes=1)))
    db.commit()
    assert student.post(f"/api/chats/{chat_id}/actions/{second['id']}/confirm").status_code == 409
    assert internet.google.inserted == []


def test_calendar_write_failure_leaves_action_retryable(client, internet):
    student = ready(client, internet, google=True, write=True)
    use_llm(ScriptedLLM([("create_event", {"title": "Study", "start": "2026-11-10T10:00"})]))
    chat_id, events = ask(student, "Block study time")
    action = next(d for k, d in events if k == "action")
    internet.google.insert_forbidden = True
    res = student.post(f"/api/chats/{chat_id}/actions/{action['id']}/confirm")
    assert res.status_code == 409 and "adding events" in res.json()["detail"]
    internet.google.insert_forbidden = False
    assert student.post(f"/api/chats/{chat_id}/actions/{action['id']}/confirm").status_code == 200


# --- Privacy between students -------------------------------------------------------------------------


def test_a_student_can_never_see_another_students_data(client, internet):
    ann = ready(client, internet, google=True, write=True)
    use_llm(ScriptedLLM([("create_event", {"title": "Ann's study", "start": "2026-11-10T10:00"})]))
    ann_chat, events = ask(ann, "Add study time")
    ann_action = next(d for k, d in events if k == "action")

    bob = register(new_client(), email="bob@school.edu")
    every_tool = [
        ("get_assignments", {"status": "all"}),
        ("get_grades", {}),
        ("get_announcements", {"days": 60}),
        ("search_course_content", {"query": "midterm"}),
        ("get_events", {}),
        ("find_free_time", {}),
    ]
    llm = ScriptedLLM(every_tool)
    use_llm(llm)
    ask(bob, "Show me everything")
    blob = json.dumps(llm.results())
    for secret in (
        "Organic Chemistry",
        "Problem Set 5",
        "Midterm logistics",
        "Lecture",
        "Review session",
    ):
        assert secret not in blob, secret

    # Ann's chat, messages and actions are invisible and untouchable for Bob.
    assert bob.get(f"/api/chats/{ann_chat}").status_code == 404
    assert bob.post(f"/api/chats/{ann_chat}/messages", json={"content": "hi"}).status_code == 404
    assert bob.post(f"/api/chats/{ann_chat}/actions/{ann_action['id']}/confirm").status_code == 404
    assert bob.delete(f"/api/chats/{ann_chat}").status_code == 404
    assert all(c["id"] != ann_chat for c in bob.get("/api/chats").json())


def test_chat_requires_login_and_verified_email(client, internet):
    anon = new_client()
    assert anon.post("/api/chats").status_code == 401
    unverified = register(client, verify=False)
    assert unverified.post("/api/chats").status_code == 403


# --- Limits, failures, history -------------------------------------------------------------------------


def test_daily_token_cap(client, internet, db):
    student = ready(client, internet)
    use_llm(ScriptedLLM([]))
    ask(student, "hi")
    usage = db.scalar(select(LLMUsage))
    assert usage.input_tokens == 200 and usage.output_tokens == 20
    db.execute(update(LLMUsage).values(input_tokens=200_000))
    db.commit()
    chat = student.post("/api/chats").json()["id"]
    res = student.post(f"/api/chats/{chat}/messages", json={"content": "again"})
    assert res.status_code == 429 and "limit" in res.json()["detail"]


def test_provider_failure_streams_error_and_saves_nothing(client, internet, db):
    class Broken(LLMClient):
        provider = model = "broken"

        async def stream_turn(self, system, messages, tools):  # noqa: ANN001, ANN201
            raise LLMError("upstream down")
            yield  # pragma: no cover

    student = ready(client, internet)
    use_llm(Broken())
    _, events = ask(student, "hi")
    assert events == [
        ("error", {"detail": "The AI service didn't respond. Try again in a moment."})
    ]
    assert db.scalar(select(func.count()).select_from(Message)) == 0


def test_history_is_sent_and_trimmed(client, internet):
    student = ready(client, internet)
    llm = ScriptedLLM([])
    use_llm(llm)
    chat_id, _ = ask(student, "first question")
    ask(student, "second question", chat_id)
    sent = llm.rounds[-1]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"] == "first question" and sent[-1]["content"] == "second question"


def test_overlong_and_empty_messages_rejected(client, internet):
    student = ready(client, internet)
    chat = student.post("/api/chats").json()["id"]
    assert (
        student.post(f"/api/chats/{chat}/messages", json={"content": "x" * 8001}).status_code == 422
    )
    assert student.post(f"/api/chats/{chat}/messages", json={"content": "   "}).status_code == 422


def test_stale_data_is_refreshed_before_answering(client, internet, db):
    student = ready(client, internet)
    db.execute(update(Connection).values(last_synced_at=utcnow() - timedelta(hours=3)))
    db.commit()
    calls = len(internet.canvas.calls)
    use_llm(ScriptedLLM([("get_assignments", {})]))
    _, events = ask(student, "What's due?")
    labels = [d["label"] for k, d in events if k == "tool"]
    assert labels == ["Checking your Canvas assignments", "Refreshing your Canvas data"]
    assert len(internet.canvas.calls) > calls


def test_tool_rounds_are_capped(client, internet):
    class Looping(LLMClient):
        provider = model = "loop"

        def __init__(self) -> None:
            self.tools_offered: list[int] = []

        async def stream_turn(self, system, messages, tools):  # noqa: ANN001, ANN201
            self.tools_offered.append(len(tools))
            if tools:
                yield TurnEnd(
                    text="",
                    tool_calls=[
                        ToolCall(id=f"t{len(self.tools_offered)}", name="get_grades", arguments={})
                    ],
                )
            else:
                yield TextDelta("Done.")
                yield TurnEnd(text="Done.")

    student = ready(client, internet)
    llm = Looping()
    use_llm(llm)
    _, events = ask(student, "loop forever")
    assert llm.tools_offered == [7, 7, 7, 7, 7, 7, 0]  # 6 tool rounds, then it must answer
    assert events[-1][0] == "done"


def test_list_and_delete_chats(client, internet, db):
    student = ready(client, internet)
    use_llm(ScriptedLLM([]))
    first, _ = ask(student, "one")
    second, _ = ask(student, "two")
    assert [c["id"] for c in student.get("/api/chats").json()] == [second, first]
    assert student.delete(f"/api/chats/{first}").status_code == 204
    assert [c["id"] for c in student.get("/api/chats").json()] == [second]
    assert db.scalar(select(func.count()).select_from(Message)) == 2
