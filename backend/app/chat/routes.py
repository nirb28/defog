"""Chats: create, list, read, delete, send a message (streamed), confirm or cancel actions."""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DB
from starlette.concurrency import run_in_threadpool

from ..ai import get_llm
from ..auth.sessions import require_ready_user
from ..config import Settings, get_settings
from ..connections import gcal
from ..connections.sync import user_zone
from ..connections.tokens import GOOGLE_CALENDAR, NeedsAttention, access_token, mark_needs_attention
from ..db import get_db
from ..llm import LLMClient
from ..models import CalendarEvent, Chat, Connection, Message, PendingAction, User, utcnow
from .service import message_out, prepare_turn, stream_turn
from .timeutil import fmt
from .tools import action_card

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chats", tags=["chat"])


class ChatOut(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ChatDetail(ChatOut):
    messages: list[dict]


class SendMessage(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)


def _owned_chat(db: DB, user: User, chat_id: uuid.UUID) -> Chat:
    chat = db.scalar(select(Chat).where(Chat.id == chat_id, Chat.user_id == user.id))
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found.")
    return chat


def _chat_out(chat: Chat) -> ChatOut:
    return ChatOut(
        id=chat.id, title=chat.title, created_at=chat.created_at, updated_at=chat.updated_at
    )


@router.post("", response_model=ChatOut, status_code=201)
def create_chat(user: User = Depends(require_ready_user), db: DB = Depends(get_db)) -> ChatOut:
    chat = Chat(user_id=user.id)
    db.add(chat)
    db.commit()
    return _chat_out(chat)


@router.get("", response_model=list[ChatOut])
def list_chats(user: User = Depends(require_ready_user), db: DB = Depends(get_db)) -> list[ChatOut]:
    chats = db.scalars(
        select(Chat).where(Chat.user_id == user.id).order_by(Chat.updated_at.desc()).limit(100)
    )
    return [_chat_out(c) for c in chats]


@router.get("/{chat_id}", response_model=ChatDetail)
def get_chat(
    chat_id: uuid.UUID, user: User = Depends(require_ready_user), db: DB = Depends(get_db)
) -> ChatDetail:
    chat = _owned_chat(db, user, chat_id)
    zone = user_zone(user)
    messages = list(
        db.scalars(select(Message).where(Message.chat_id == chat.id).order_by(Message.seq))
    )
    actions: dict[uuid.UUID, list[dict]] = {}
    for a in db.scalars(
        select(PendingAction)
        .where(PendingAction.chat_id == chat.id, PendingAction.user_id == user.id)
        .order_by(PendingAction.created_at)
    ):
        if a.message_id:
            actions.setdefault(a.message_id, []).append(action_card(a, zone))
    return ChatDetail(
        **_chat_out(chat).model_dump(),
        messages=[message_out(m, actions.get(m.id)) for m in messages],
    )


@router.delete("/{chat_id}", status_code=204)
def delete_chat(
    chat_id: uuid.UUID, user: User = Depends(require_ready_user), db: DB = Depends(get_db)
) -> None:
    db.delete(_owned_chat(db, user, chat_id))  # cascades to messages and pending actions
    db.commit()


@router.post("/{chat_id}/messages")
async def send_message(
    chat_id: uuid.UUID,
    body: SendMessage,
    user: User = Depends(require_ready_user),
    settings: Settings = Depends(get_settings),
    llm: LLMClient = Depends(get_llm),
) -> StreamingResponse:
    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="Type a message first.")
    setup = await run_in_threadpool(prepare_turn, settings, user.id, chat_id, content)
    return StreamingResponse(
        stream_turn(settings, llm, setup, content),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Confirmable actions ---------------------------------------------------------------------------


def _owned_action(db: DB, user: User, chat_id: uuid.UUID, action_id: uuid.UUID) -> PendingAction:
    action = db.scalar(
        select(PendingAction)
        .where(
            PendingAction.id == action_id,
            PendingAction.chat_id == chat_id,
            PendingAction.user_id == user.id,
        )
        .with_for_update()
    )
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found.")
    if action.status != "pending":
        raise HTTPException(status_code=409, detail=f"This action is already {action.status}.")
    if action.expires_at <= utcnow():
        raise HTTPException(status_code=409, detail="This suggestion expired. Ask again.")
    return action


def _follow_up(db: DB, user: User, chat: Chat, text: str) -> Message:
    message = Message(
        chat_id=chat.id,
        user_id=user.id,
        role="assistant",
        content=text,
        sources=["Google Calendar"],
    )
    db.add(message)
    chat.updated_at = utcnow()
    return message


@router.post("/{chat_id}/actions/{action_id}/confirm")
def confirm_action(
    chat_id: uuid.UUID,
    action_id: uuid.UUID,
    user: User = Depends(require_ready_user),
    db: DB = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    chat = _owned_chat(db, user, chat_id)
    action = _owned_action(db, user, chat_id, action_id)
    zone = user_zone(user)
    if action.kind != "create_event":
        raise HTTPException(status_code=400, detail="Unknown action.")

    conn = db.scalar(
        select(Connection).where(
            Connection.user_id == user.id, Connection.provider == GOOGLE_CALENDAR
        )
    )
    if conn is None:
        raise HTTPException(status_code=409, detail="Connect Google Calendar first.")
    if not (conn.settings or {}).get("allow_writes") or gcal.WRITE_SCOPE not in conn.scopes.split():
        raise HTTPException(
            status_code=409,
            detail="Turn on “Let Defog add events” on the Connections page, then confirm again.",
        )

    p = action.payload
    start = datetime.fromisoformat(p["start"])
    end = datetime.fromisoformat(p["end"])
    event_body = {
        "summary": p["title"],
        "description": ((p.get("description") or "") + "\n\nAdded by Defog.").strip(),
        "start": {"dateTime": start.astimezone(zone).isoformat(), "timeZone": str(zone)},
        "end": {"dateTime": end.astimezone(zone).isoformat(), "timeZone": str(zone)},
    }
    # Claim the action in its own commit, so a double-click can't add the event twice.
    action.status = "running"
    db.commit()

    def release(status_code: int, detail: str | None) -> HTTPException:
        db.rollback()
        db.execute(
            update(PendingAction).where(PendingAction.id == action_id).values(status="pending")
        )
        db.commit()
        return HTTPException(status_code=status_code, detail=detail)

    try:
        token = access_token(db, conn, settings)
        try:
            with gcal.CalendarAPI(token) as api:
                created = api.insert_event("primary", event_body)
        except gcal.CalendarAuthError:
            token = access_token(db, conn, settings, force_refresh=True)
            with gcal.CalendarAPI(token) as api:
                created = api.insert_event("primary", event_body)
    except NeedsAttention as exc:
        raise release(409, str(exc))
    except gcal.CalendarAuthError:
        mark_needs_attention(db, conn)
        raise release(409, conn.status_detail)
    except gcal.CalendarScopeError:
        raise release(
            409,
            "Google didn't allow Defog to add events. Reconnect Google Calendar with adding events turned on.",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Calendar insert failed: %s", exc)
        raise release(502, "Google Calendar didn't respond. Try again.")

    action.status = "done"
    action.result = {"event_id": created.get("id"), "url": created.get("htmlLink")}
    # Show it in Defog straight away instead of waiting for the next sync.
    calendar_ids = (conn.settings or {}).get("calendar_ids") or ["primary"]
    if "primary" in calendar_ids and created.get("id"):
        exists = db.scalar(
            select(CalendarEvent.id).where(
                CalendarEvent.connection_id == conn.id,
                CalendarEvent.calendar_id == "primary",
                CalendarEvent.external_id == created["id"],
            )
        )
        if exists is None:
            db.add(
                CalendarEvent(
                    user_id=user.id,
                    connection_id=conn.id,
                    source=GOOGLE_CALENDAR,
                    external_id=created["id"],
                    calendar_id="primary",
                    title=p["title"],
                    start_at=start,
                    end_at=end,
                    html_url=created.get("htmlLink"),
                )
            )
    link = f" [Open in Google Calendar]({created['htmlLink']})" if created.get("htmlLink") else ""
    message = _follow_up(
        db,
        user,
        chat,
        f"Added **{p['title']}** to your Google Calendar on {fmt(start, zone)}.{link}",
    )
    db.commit()
    return {"action": action_card(action, zone), "message": message_out(message)}


@router.post("/{chat_id}/actions/{action_id}/cancel")
def cancel_action(
    chat_id: uuid.UUID,
    action_id: uuid.UUID,
    user: User = Depends(require_ready_user),
    db: DB = Depends(get_db),
) -> dict:
    _owned_chat(db, user, chat_id)
    action = _owned_action(db, user, chat_id, action_id)
    action.status = "cancelled"
    db.commit()
    return {"action": action_card(action, user_zone(user))}
