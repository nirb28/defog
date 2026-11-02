"""AI providers behind one small interface, so the rest of the app doesn't care which is used.

Every provider streams a turn: text as it's written, then a TurnEnd with any tool calls the
model wants to make. The chat service runs those tools and calls the provider again.

Conversation messages use one neutral format:
    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall], "raw": provider data}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""

import json
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .config import Settings


class LLMError(Exception):
    """Raised when the AI provider can't produce a reply."""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema for the arguments


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class TextDelta:
    text: str


@dataclass
class TurnEnd:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None  # provider-specific assistant content, replayed within the same turn
    input_tokens: int = 0
    output_tokens: int = 0


def estimate_tokens(text: str) -> int:
    """Rough count (about 4 characters per token) for providers that don't report usage."""
    return max(1, len(text) // 4)


def _parse_arguments(raw: str) -> dict:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"_invalid_arguments": raw[:500]}
    return value if isinstance(value, dict) else {"_invalid_arguments": raw[:500]}


class LLMClient(ABC):
    provider: str
    model: str

    @abstractmethod
    def stream_turn(
        self, system: str, messages: list[dict], tools: list[ToolSpec]
    ) -> AsyncIterator[TextDelta | TurnEnd]:
        """Yield TextDelta chunks, then exactly one TurnEnd."""


# --- Mock --------------------------------------------------------------------------------------


class MockClient(LLMClient):
    """Answers without calling any API, so the app runs with no key and no cost.

    It picks a data tool from keywords in the question, then lists what the tool returned,
    which is enough to try the whole app end to end.
    """

    provider = "mock"
    model = "mock"

    KEYWORDS: list[tuple[tuple[str, ...], str]] = [
        (("free", "available", "availability"), "find_free_time"),
        (("grade", "doing in", "score"), "get_grades"),
        (("announcement", "announce"), "get_announcements"),
        (("due", "assignment", "deadline", "homework"), "get_assignments"),
        (("schedule", "calendar", "event", "busy"), "get_events"),
    ]

    async def stream_turn(self, system, messages, tools):  # noqa: ANN001, ANN201
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
        tool_results = [m for m in messages[last_user + 1 :] if m["role"] == "tool"]
        question = messages[last_user]["content"].strip()
        available = {t.name for t in tools}

        if not tool_results:
            lowered = question.lower()
            for words, tool in self.KEYWORDS:
                if tool in available and any(w in lowered for w in words):
                    call = ToolCall(id=f"mock_{tool}", name=tool, arguments={})
                    yield TurnEnd(text="", tool_calls=[call], raw=None)
                    return
            text = (
                "This is a **mock reply**: the backend is running, but no AI provider is set up yet.\n\n"
                f"You asked: _{question[:300]}_\n\n"
                "Try asking what's due this week, how your grades look, or when you're free. "
                "To get real answers, set `LLM_PROVIDER` and an API key in `backend/.env`."
            )
        else:
            text = "Here's what I found (mock mode, so this is a plain listing):\n\n" + "\n\n".join(
                _render_tool_result(json.loads(m["content"])) for m in tool_results
            )

        for chunk in re.findall(r"\S+\s*", text):
            yield TextDelta(chunk)
        yield TurnEnd(
            text=text, input_tokens=estimate_tokens(question), output_tokens=estimate_tokens(text)
        )


def _render_tool_result(result: dict) -> str:
    if result.get("error"):
        return f"- {result['error']}"
    items = result.get("items") or result.get("courses") or result.get("slots") or []
    if not items:
        return "- Nothing found."
    lines = []
    for item in items[:15]:
        if "minutes" in item:  # a free-time slot
            lines.append(
                f"- **{item['start']}** to {item['end'].split(', ')[-1]} ({item['minutes']} min free)"
            )
            continue
        title = item.get("title") or item.get("name") or item.get("course") or "Item"
        when = item.get("due") or item.get("start") or item.get("posted") or ""
        extra = item.get("course") if item.get("title") else item.get("grade") or ""
        parts = [p for p in (f"**{title}**", str(when), str(extra or "")) if p]
        lines.append("- " + " · ".join(parts))
    return "\n".join(lines)


# --- Anthropic (Claude) ------------------------------------------------------------------------


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int) -> None:
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set.")
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)
        self.model = model
        self._max_tokens = max_tokens

    @staticmethod
    def _messages(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                if m.get("raw"):
                    out.append({"role": "assistant", "content": m["raw"]})
                elif m.get("content"):
                    out.append({"role": "assistant", "content": m["content"]})
            elif m["role"] == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": m["content"],
                }
                # All results for one assistant turn go in a single user message.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    async def stream_turn(self, system, messages, tools):  # noqa: ANN001, ANN201
        from anthropic import APIError

        kwargs: dict = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": self._messages(messages),
            "stream": True,
        }
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]

        blocks: dict[int, dict] = {}
        json_parts: dict[int, list[str]] = {}
        input_tokens = output_tokens = 0
        try:
            stream = await self._client.messages.create(**kwargs)
            async for event in stream:
                kind = event.type
                if kind == "message_start":
                    usage = getattr(event.message, "usage", None)
                    input_tokens = getattr(usage, "input_tokens", 0) or 0
                elif kind == "content_block_start":
                    block = event.content_block.model_dump(exclude_none=True)
                    if block["type"] == "tool_use":
                        block["input"] = {}
                        json_parts[event.index] = []
                    elif block["type"] == "text":
                        block["text"] = block.get("text", "")
                    elif block["type"] == "thinking":
                        block.setdefault("thinking", "")
                        block.setdefault("signature", "")
                    blocks[event.index] = block
                elif kind == "content_block_delta":
                    delta = event.delta
                    block = blocks.get(event.index)
                    if block is None:
                        continue
                    if delta.type == "text_delta":
                        block["text"] += delta.text
                        yield TextDelta(delta.text)
                    elif delta.type == "input_json_delta":
                        json_parts[event.index].append(delta.partial_json)
                    elif delta.type == "thinking_delta":
                        block["thinking"] += delta.thinking
                    elif delta.type == "signature_delta":
                        block["signature"] += delta.signature
                elif kind == "message_delta":
                    usage = getattr(event, "usage", None)
                    output_tokens = getattr(usage, "output_tokens", 0) or output_tokens
        except APIError as exc:
            raise LLMError(f"Anthropic API error: {exc}") from exc

        calls: list[ToolCall] = []
        raw: list[dict] = []
        for index in sorted(blocks):
            block = blocks[index]
            if block["type"] == "tool_use":
                block["input"] = _parse_arguments("".join(json_parts.get(index, [])))
                calls.append(ToolCall(id=block["id"], name=block["name"], arguments=block["input"]))
            # Drop fields the API doesn't accept back (e.g. citations=None already excluded).
            raw.append(block)
        text = "".join(b.get("text", "") for b in raw if b["type"] == "text")
        if not text and not calls:
            raise LLMError("The model returned an empty reply.")
        yield TurnEnd(
            text=text,
            tool_calls=calls,
            raw=raw,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


# --- OpenAI-compatible ---------------------------------------------------------------------------


class OpenAICompatibleClient(LLMClient):
    """Works with OpenAI, Azure OpenAI, or a local Ollama server (set OPENAI_BASE_URL)."""

    provider = "openai"

    def __init__(self, api_key: str, base_url: str, model: str, max_tokens: int) -> None:
        if not model:
            raise LLMError("OPENAI_MODEL is not set.")
        from openai import AsyncOpenAI

        # Local servers such as Ollama accept any key, so fall back to a placeholder.
        self._client = AsyncOpenAI(api_key=api_key or "not-needed", base_url=base_url or None)
        self.model = model
        self._max_tokens = max_tokens

    @staticmethod
    def _messages(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                item: dict = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    item["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in m["tool_calls"]
                    ]
                if item["content"] is not None or item.get("tool_calls"):
                    out.append(item)
            elif m["role"] == "tool":
                out.append(
                    {"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]}
                )
        return out

    async def stream_turn(self, system, messages, tools):  # noqa: ANN001, ANN201
        from openai import OpenAIError

        kwargs: dict = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "messages": self._messages(system, messages),
            "stream": True,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]

        text_parts: list[str] = []
        calls: dict[int, dict] = {}
        input_tokens = output_tokens = 0
        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage:
                    input_tokens = usage.prompt_tokens or input_tokens
                    output_tokens = usage.completion_tokens or output_tokens
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                if delta.content:
                    text_parts.append(delta.content)
                    yield TextDelta(delta.content)
                for tc in delta.tool_calls or []:
                    slot = calls.setdefault(
                        tc.index if tc.index is not None else len(calls),
                        {"id": "", "name": "", "args": []},
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function is not None:
                        if tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function.arguments:
                            slot["args"].append(tc.function.arguments)
        except OpenAIError as exc:
            raise LLMError(f"OpenAI-compatible API error: {exc}") from exc

        text = "".join(text_parts)
        tool_calls = [
            ToolCall(
                id=slot["id"] or f"call_{i}",
                name=slot["name"],
                arguments=_parse_arguments("".join(slot["args"])),
            )
            for i, slot in sorted(calls.items())
            if slot["name"]
        ]
        if not text and not tool_calls:
            raise LLMError("The model returned an empty reply.")
        if not input_tokens:
            input_tokens = estimate_tokens(json.dumps(kwargs["messages"]))
        if not output_tokens:
            output_tokens = estimate_tokens(text + json.dumps([c.arguments for c in tool_calls]))
        yield TurnEnd(
            text=text, tool_calls=tool_calls, input_tokens=input_tokens, output_tokens=output_tokens
        )


def build_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "anthropic":
        return AnthropicClient(
            settings.anthropic_api_key, settings.anthropic_model, settings.max_output_tokens
        )
    if settings.llm_provider == "openai":
        return OpenAICompatibleClient(
            settings.openai_api_key,
            settings.openai_base_url,
            settings.openai_model,
            settings.max_output_tokens,
        )
    return MockClient()
