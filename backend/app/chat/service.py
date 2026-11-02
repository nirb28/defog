"""One chat turn: stream the model's answer, running data tools as it asks for them.

The answer streams to the browser as Server-Sent Events:
    event: delta   data: {"text": "..."}              a piece of the answer
    event: tool    data: {"name": ..., "label": ...}   a tool is running ("Checking your calendar")
    event: action  data: {action card}                 a calendar write awaiting confirmation
    event: done    data: {"message": {...}}            the saved assistant message
    event: error   data: {"detail": "..."}             nothing was saved; the student can retry
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from starlette.concurrency import run_in_threadpool

from ..config import Settings
from ..connections.sync import is_stale, sync_connection, user_zone
from ..connections.tokens import CANVAS, GOOGLE_CALENDAR, PROVIDER_NAMES
from ..db import session_scope
from ..llm import LLMClient, LLMError, TextDelta, TurnEnd
from ..models import Chat, Connection, LLMUsage, Message, PendingAction, User, utcnow
from ..prompts import DATA_PROMPT, SYSTEM_PROMPT
from .timeutil import fmt
from .tools import READS, SOURCE_LABELS, TOOL_SPECS, ToolContext, action_card, run_tool, tool_label

log = logging.getLogger(__name__)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@dataclass
class TurnSetup:
    user_id: uuid.UUID
    chat_id: uuid.UUID
    system: str
    history: list[dict]
    first_message: bool


def today_usage(db, user_id: uuid.UUID) -> int:  # noqa: ANN001
    row = db.get(LLMUsage, (user_id, utc_today()))
    return (row.input_tokens + row.output_tokens) if row else 0


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _sources_line(connections: list[Connection], zone) -> str:  # noqa: ANN001
    parts = []
    for provider in (CANVAS, GOOGLE_CALENDAR):
        conn = next((c for c in connections if c.provider == provider), None)
        name = PROVIDER_NAMES[provider]
        if conn is None:
            parts.append(f"{name}: not connected")
        elif conn.status == "needs_attention":
            parts.append(f"{name}: needs reconnecting (data may be out of date)")
        else:
            synced = fmt(conn.last_synced_at, zone) or "first sync in progress"
            parts.append(f"{name}: connected (updated {synced})")
    return "; ".join(parts)


def prepare_turn(
    settings: Settings, user_id: uuid.UUID, chat_id: uuid.UUID, content: str
) -> TurnSetup:
    """Checks that run before streaming starts, so they can still return a normal HTTP error."""
    if len(content) > settings.max_message_chars:
        raise HTTPException(
            status_code=422,
            detail=f"Messages can be at most {settings.max_message_chars} characters.",
        )
    with session_scope() as db:
        user = db.get(User, user_id)
        chat = db.scalar(select(Chat).where(Chat.id == chat_id, Chat.user_id == user_id))
        if user is None or chat is None:
            raise HTTPException(status_code=404, detail="Chat not found.")
        if today_usage(db, user_id) >= settings.daily_token_limit:
            raise HTTPException(
                status_code=429,
                detail="You've reached today's chat limit. It resets at midnight UTC.",
            )
        rows = list(
            db.scalars(
                select(Message)
                .where(Message.chat_id == chat_id, Message.user_id == user_id)
                .order_by(Message.seq.desc())
                .limit(settings.max_history_messages - 1)
            )
        )[::-1]
        history = [{"role": m.role, "content": m.content} for m in rows if m.content]
        # The provider APIs need the conversation to start with the student.
        while history and history[0]["role"] != "user":
            history.pop(0)
        zone = user_zone(user)
        connections = list(db.scalars(select(Connection).where(Connection.user_id == user_id)))
        now = utcnow().astimezone(zone)
        system = SYSTEM_PROMPT + DATA_PROMPT.format(
            name=user.name.split()[0] if user.name.strip() else "The student",
            now=f"{now:%A}, {now:%B} {now.day}, {now.year}, {now.hour % 12 or 12}:{now:%M} {now:%p}",
            zone=str(zone),
            sources=_sources_line(connections, zone),
            app_url=settings.app_url,
        )
        return TurnSetup(
            user_id=user_id,
            chat_id=chat_id,
            system=system,
            history=history,
            first_message=not rows,
        )


def _run_tool_sync(
    settings: Settings, setup: TurnSetup, name: str, args: dict
) -> tuple[dict, list[dict]]:
    with session_scope() as db:
        user = db.get(User, setup.user_id)
        assert user is not None
        ctx = ToolContext(db=db, user=user, settings=settings, chat_id=setup.chat_id)
        result = run_tool(ctx, name, args)
        db.commit()
        return result, ctx.actions


def _stale_connections(
    settings: Settings, user_id: uuid.UUID, providers: tuple[str, ...]
) -> list[tuple[uuid.UUID, str]]:
    if not providers:
        return []
    with session_scope() as db:
        conns = db.scalars(
            select(Connection).where(
                Connection.user_id == user_id,
                Connection.provider.in_(providers),
                Connection.status == "connected",
            )
        )
        return [(c.id, c.provider) for c in conns if is_stale(c, settings.chat_sync_stale_minutes)]


def _save_turn(
    settings: Settings,
    setup: TurnSetup,
    content: str,
    answer: str,
    tools_used: list[dict],
    action_ids: list[str],
    input_tokens: int,
    output_tokens: int,
) -> dict:
    with session_scope() as db:
        chat = db.scalar(
            select(Chat).where(Chat.id == setup.chat_id, Chat.user_id == setup.user_id)
        )
        if chat is None:  # deleted while the answer was streaming
            return {}
        db.add(Message(chat_id=chat.id, user_id=setup.user_id, role="user", content=content))
        db.flush()
        sources = list(
            dict.fromkeys(
                SOURCE_LABELS[t["name"]] for t in tools_used if t["name"] in SOURCE_LABELS
            )
        )
        reply = Message(
            chat_id=chat.id,
            user_id=setup.user_id,
            role="assistant",
            content=answer,
            tool_calls=tools_used,
            sources=sources,
        )
        db.add(reply)
        db.flush()
        if action_ids:
            db.execute(
                update(PendingAction)
                .where(
                    PendingAction.id.in_([uuid.UUID(a) for a in action_ids]),
                    PendingAction.user_id == setup.user_id,
                )
                .values(message_id=reply.id)
            )
        if setup.first_message or chat.title == "New chat":
            title = " ".join(content.split())
            chat.title = title[:60] + ("…" if len(title) > 60 else "")
        chat.updated_at = utcnow()
        stmt = insert(LLMUsage).values(
            user_id=setup.user_id,
            day=utc_today(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=[LLMUsage.user_id, LLMUsage.day],
                set_={
                    "input_tokens": LLMUsage.input_tokens + stmt.excluded.input_tokens,
                    "output_tokens": LLMUsage.output_tokens + stmt.excluded.output_tokens,
                },
            )
        )
        db.commit()
        zone = user_zone(db.get(User, setup.user_id))
        actions = [
            action_card(a, zone)
            for a in db.scalars(select(PendingAction).where(PendingAction.message_id == reply.id))
        ]
        return message_out(reply, actions) | {"chat_title": chat.title}


def message_out(message: Message, actions: list[dict] | None = None) -> dict:
    return {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "sources": message.sources or [],
        "actions": actions or [],
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


async def stream_turn(
    settings: Settings, llm: LLMClient, setup: TurnSetup, content: str
) -> AsyncIterator[str]:
    conversation = [*setup.history, {"role": "user", "content": content}]
    answer: list[str] = []
    tools_used: list[dict] = []
    action_ids: list[str] = []
    refreshed: set[uuid.UUID] = set()
    input_tokens = output_tokens = 0

    try:
        for round_no in range(settings.max_tool_rounds + 1):
            # On the last round the model must answer with what it has.
            tools = TOOL_SPECS if round_no < settings.max_tool_rounds else []
            needs_gap = bool("".join(answer).strip())
            end: TurnEnd | None = None
            async for event in llm.stream_turn(setup.system, conversation, tools):
                if isinstance(event, TextDelta):
                    if needs_gap and event.text.strip():
                        answer.append("\n\n")
                        yield sse("delta", {"text": "\n\n"})
                        needs_gap = False
                    answer.append(event.text)
                    yield sse("delta", {"text": event.text})
                else:
                    end = event
            assert end is not None
            input_tokens += end.input_tokens
            output_tokens += end.output_tokens
            if not end.tool_calls:
                break

            conversation.append(
                {
                    "role": "assistant",
                    "content": end.text,
                    "tool_calls": end.tool_calls,
                    "raw": end.raw,
                }
            )
            for call in end.tool_calls:
                yield sse("tool", {"name": call.name, "label": tool_label(call.name)})
                for conn_id, provider in await run_in_threadpool(
                    _stale_connections, settings, setup.user_id, READS.get(call.name, ())
                ):
                    if conn_id in refreshed:
                        continue
                    refreshed.add(conn_id)
                    yield sse(
                        "tool",
                        {
                            "name": "sync",
                            "label": f"Refreshing your {PROVIDER_NAMES[provider]} data",
                        },
                    )
                    await run_in_threadpool(sync_connection, conn_id)
                result, actions = await run_in_threadpool(
                    _run_tool_sync, settings, setup, call.name, call.arguments
                )
                for card in actions:
                    action_ids.append(card["id"])
                    yield sse("action", card)
                tools_used.append(
                    {"name": call.name, "arguments": call.arguments, "ok": "error" not in result}
                )
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": json.dumps(result, default=str),
                    }
                )
    except LLMError as exc:
        log.warning("LLM failure: %s", exc)
        yield sse("error", {"detail": "The AI service didn't respond. Try again in a moment."})
        return
    except Exception:  # noqa: BLE001 - the stream has started, so report instead of raising
        log.exception("Chat turn failed")
        yield sse("error", {"detail": "Something went wrong on our side. Try again."})
        return

    text = "".join(answer).strip()
    if not text:
        text = "Sorry, I couldn't put together an answer. Try asking again."
    saved = await run_in_threadpool(
        _save_turn,
        settings,
        setup,
        content,
        text,
        tools_used,
        action_ids,
        input_tokens,
        output_tokens,
    )
    yield sse("done", {"message": saved})
