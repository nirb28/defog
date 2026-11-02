"""Provider adapters: streaming, tool calls and message formats, against recorded API streams."""

import asyncio
import json

import httpx2 as httpx
import pytest

from app.llm import (
    AnthropicClient,
    LLMError,
    MockClient,
    OpenAICompatibleClient,
    TextDelta,
    ToolCall,
    ToolSpec,
    TurnEnd,
)

TOOLS = [
    ToolSpec(
        "get_assignments",
        "List assignments",
        {"type": "object", "properties": {"status": {"type": "string"}}},
    )
]


def sse(events: list[tuple[str | None, dict | str]]) -> str:
    out = []
    for name, data in events:
        payload = data if isinstance(data, str) else json.dumps(data)
        out.append((f"event: {name}\n" if name else "") + f"data: {payload}\n\n")
    return "".join(out)


def collect(gen) -> tuple[list[str], TurnEnd]:  # noqa: ANN001
    async def run():
        deltas, end = [], None
        async for ev in gen:
            if isinstance(ev, TextDelta):
                deltas.append(ev.text)
            else:
                end = ev
        return deltas, end

    return asyncio.run(run())


# --- Anthropic ------------------------------------------------------------------------------------

ANTHROPIC_TOOL_STREAM = [
    (
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 25, "output_tokens": 1},
            },
        },
    ),
    (
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Need their data."},
        },
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig-abc"},
        },
    ),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    (
        "content_block_start",
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "Checking "},
        },
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "Canvas."},
        },
    ),
    ("content_block_stop", {"type": "content_block_stop", "index": 1}),
    (
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "get_assignments",
                "input": {},
            },
        },
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"status": "up'},
        },
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": 'coming"}'},
        },
    ),
    ("content_block_stop", {"type": "content_block_stop", "index": 2}),
    (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 42},
        },
    ),
    ("message_stop", {"type": "message_stop"}),
]


def anthropic_with(stream_text: str, sent: list) -> AnthropicClient:
    from anthropic import AsyncAnthropic

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, text=stream_text, headers={"content-type": "text/event-stream"})

    llm = AnthropicClient("test-key", "claude-haiku-5-5", 256)
    llm._client = AsyncAnthropic(
        api_key="test-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return llm


def test_anthropic_streams_text_and_tool_calls():
    sent: list = []
    llm = anthropic_with(sse(ANTHROPIC_TOOL_STREAM), sent)
    deltas, end = collect(
        llm.stream_turn("system prompt", [{"role": "user", "content": "What's due?"}], TOOLS)
    )
    assert deltas == ["Checking ", "Canvas."]
    assert end.text == "Checking Canvas."
    assert end.tool_calls == [
        ToolCall(id="toolu_1", name="get_assignments", arguments={"status": "upcoming"})
    ]
    assert (end.input_tokens, end.output_tokens) == (25, 42)
    # Thinking blocks are kept, signature included, to send back within the same turn.
    assert end.raw[0] == {
        "type": "thinking",
        "thinking": "Need their data.",
        "signature": "sig-abc",
    }
    request = sent[0]
    assert (
        request["stream"] is True
        and request["system"] == "system prompt"
        and request["max_tokens"] == 256
    )
    assert (
        request["tools"][0]["name"] == "get_assignments" and "input_schema" in request["tools"][0]
    )


def test_anthropic_tool_results_go_back_in_one_user_message():
    msgs = AnthropicClient._messages(
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "Checking",
                "tool_calls": [],
                "raw": [{"type": "text", "text": "Checking"}],
            },
            {"role": "tool", "tool_call_id": "a", "name": "x", "content": "{}"},
            {"role": "tool", "tool_call_id": "b", "name": "y", "content": "{}"},
        ]
    )
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["a", "b"]
    assert msgs[1]["content"] == [{"type": "text", "text": "Checking"}]


def test_anthropic_api_errors_become_llm_errors():
    from anthropic import AsyncAnthropic

    def handler(request):  # noqa: ANN001
        return httpx.Response(
            529,
            json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        )

    llm = AnthropicClient("k", "claude-haiku-5-5", 256)
    llm._client = AsyncAnthropic(
        api_key="k",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(LLMError):
        collect(llm.stream_turn("s", [{"role": "user", "content": "hi"}], []))


def test_anthropic_missing_key():
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicClient("", "claude-haiku-5-5", 256)


# --- OpenAI-compatible ---------------------------------------------------------------------------


def chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> dict:
    body = {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        body["usage"] = usage
    return body


def openai_with(stream_text: str, sent: list) -> OpenAICompatibleClient:
    from openai import AsyncOpenAI

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, text=stream_text, headers={"content-type": "text/event-stream"})

    llm = OpenAICompatibleClient("k", "https://api.example.com/v1", "muse-spark-1.1", 512)
    llm._client = AsyncOpenAI(
        api_key="k",
        base_url="https://api.example.com/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return llm


def test_openai_streams_tool_call_fragments():
    stream = sse(
        [
            (
                None,
                chunk(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_assignments", "arguments": ""},
                            }
                        ],
                    }
                ),
            ),
            (None, chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"status":'}}]})),
            (None, chunk({"tool_calls": [{"index": 0, "function": {"arguments": ' "missing"}'}}]})),
            (None, chunk({}, finish="tool_calls")),
            (None, "[DONE]"),
        ]
    )
    sent: list = []
    deltas, end = collect(
        openai_with(stream, sent).stream_turn("sys", [{"role": "user", "content": "hi"}], TOOLS)
    )
    assert deltas == [] and end.text == ""
    assert end.tool_calls == [
        ToolCall(id="call_1", name="get_assignments", arguments={"status": "missing"})
    ]
    assert (
        end.input_tokens > 0 and end.output_tokens > 0
    )  # estimated when the server doesn't report usage
    req = sent[0]
    assert req["model"] == "muse-spark-1.1" and req["stream"] is True and req["max_tokens"] == 512
    assert req["messages"][0] == {"role": "system", "content": "sys"}
    assert (
        req["tools"][0]["type"] == "function"
        and req["tools"][0]["function"]["name"] == "get_assignments"
    )


def test_openai_streams_text_and_reports_usage():
    stream = sse(
        [
            (None, chunk({"role": "assistant", "content": "Hel"})),
            (None, chunk({"content": "lo!"})),
            (
                None,
                chunk(
                    {},
                    finish="stop",
                    usage={"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
                ),
            ),
            (None, "[DONE]"),
        ]
    )
    deltas, end = collect(
        openai_with(stream, []).stream_turn("sys", [{"role": "user", "content": "hi"}], [])
    )
    assert deltas == ["Hel", "lo!"] and end.text == "Hello!" and end.tool_calls == []
    assert (end.input_tokens, end.output_tokens) == (11, 3)


def test_openai_message_format_for_tool_results():
    msgs = OpenAICompatibleClient._messages(
        "sys",
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [ToolCall("call_1", "get_grades", {"course": "BIOL"})],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "get_grades",
                "content": '{"courses": []}',
            },
        ],
    )
    assert msgs[2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_grades", "arguments": '{"course": "BIOL"}'},
            }
        ],
    }
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_1", "content": '{"courses": []}'}


def test_openai_empty_reply_is_an_error():
    stream = sse(
        [(None, chunk({"role": "assistant", "content": ""}, finish="stop")), (None, "[DONE]")]
    )
    with pytest.raises(LLMError, match="empty"):
        collect(openai_with(stream, []).stream_turn("sys", [{"role": "user", "content": "hi"}], []))


def test_openai_missing_model():
    with pytest.raises(LLMError, match="OPENAI_MODEL"):
        OpenAICompatibleClient("k", "", "", 256)


def test_malformed_tool_arguments_are_flagged():
    stream = sse(
        [
            (
                None,
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c",
                                "type": "function",
                                "function": {"name": "get_grades", "arguments": "{not json"},
                            }
                        ]
                    }
                ),
            ),
            (None, "[DONE]"),
        ]
    )
    _, end = collect(
        openai_with(stream, []).stream_turn("sys", [{"role": "user", "content": "hi"}], TOOLS)
    )
    assert "_invalid_arguments" in end.tool_calls[0].arguments


# --- Mock --------------------------------------------------------------------------------------------


def test_mock_picks_tools_from_keywords_and_answers_without_them():
    tools = [ToolSpec(n, "", {}) for n in ("get_assignments", "get_grades", "find_free_time")]
    _, end = collect(
        MockClient().stream_turn(
            "s", [{"role": "user", "content": "When am I free Thursday?"}], tools
        )
    )
    assert end.tool_calls[0].name == "find_free_time"
    deltas, end = collect(
        MockClient().stream_turn("s", [{"role": "user", "content": "Explain entropy"}], tools)
    )
    assert end.tool_calls == [] and "mock reply" in end.text and "".join(deltas) == end.text
