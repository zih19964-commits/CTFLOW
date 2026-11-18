"""配置、兼容接口与单次请求 runtime；HTTP 全部使用替身。"""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI

from ctflow.config import Settings, load_settings
from ctflow.models import Usage, Worker
from ctflow.provider import ModelClient, Reply
from ctflow.runtime import RuntimeResponseError, SingleRequestRuntime


def write_config(tmp_path, **values):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "workspace": ".",
                "data_dir": "data",
                **values,
            }
        ),
        encoding="utf-8",
    )
    return path


async def test_config_paths_prices_defaults_and_secret_repr(tmp_path):
    path = write_config(
        tmp_path,
        prices={"qwen3.8-max": {"input": 1, "output": 2}},
    )
    settings = load_settings(path)
    assert settings.workspace == tmp_path
    assert settings.data_dir == tmp_path / "data"
    assert settings.model == "qwen3.8-max"
    assert settings.models == ["qwen3.8-max", "qwen3.8-flash"]
    worker = Worker(
        "worker",
        "测试",
        settings.model,
        base_url="https://example.com/v1",
        api_key="private-test-key",
    )
    assert worker.api_key not in repr(worker)
    assert worker.redact("private-test-key") == "[密钥已隐藏]"
    assert "接口" not in settings.safe_summary() and "密钥" not in settings.safe_summary()
    assert not any(
        hasattr(settings, name)
        for name in ("base_url", "api_key", "extra_body", "redact", "require_api")
    )
    async with ModelClient(settings, worker) as client:
        assert client.openai.max_retries == 0
        assert client.openai.timeout == settings.timeout
        assert client.usage("qwen3.8-max", 100000, 100000).cost == pytest.approx(0.3)
        assert client.usage("unknown", 1, 1).cost is None


def test_custom_provider_model_is_configurable_without_prefix_rules(tmp_path):
    custom = "provider/custom-Model:2026"
    settings = load_settings(write_config(tmp_path, model=custom, models=["second-model"]))
    assert settings.model == custom
    assert settings.models == [custom, "second-model"]


def test_mcp_servers_and_tool_rounds_are_loaded(tmp_path):
    path = write_config(
        tmp_path,
        max_tool_rounds=3,
        mcp_servers={
            "echo": {
                "transport": "stdio",
                "command": "python",
                "args": ["server.py"],
                "env": {"MODE": "test"},
            }
        },
    )
    settings = load_settings(path)
    assert settings.max_tool_rounds == 3
    assert settings.mcp_servers["echo"]["transport"] == "stdio"


@pytest.mark.parametrize(
    "invalid",
    [
        {"concurrency": 0},
        {"max_tokens": -1},
        {"typo": 1},
        {"model": " "},
        {"models": [" "]},
        {"max_tool_rounds": 0},
        {"mcp_servers": {"bad name": {"transport": "stdio", "command": "x"}}},
        {"mcp_servers": {"echo": {"transport": "stdio"}}},
        {"mcp_servers": {"echo": {"transport": "http", "url": "ftp://example.com"}}},
        {"base_url": "https://example.com/v1"},
        {"api_key": "removed-global-key"},
        {"extra_body": {}},
    ],
)
def test_invalid_and_removed_config_fields_are_reported(tmp_path, invalid):
    with pytest.raises(ValueError):
        load_settings(write_config(tmp_path, **invalid))


async def mock_client(tmp_path, handler, protocol="chat"):
    settings = Settings(
        tmp_path / "config.json",
        tmp_path / "data",
        tmp_path,
        prices={"provider/custom-Model:2026": {"input": 1, "output": 2}},
    )
    worker = Worker(
        "worker",
        "测试",
        "provider/custom-Model:2026",
        api_key="fake-key",
        base_url="https://example.com/v1",
        extra_body={"enable_thinking": False},
        protocol=protocol,
    )
    client = ModelClient(settings, worker)
    if protocol == "anthropic":
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    else:
        await client.openai.close()
        client.openai = AsyncOpenAI(
            api_key=worker.api_key,
            base_url=worker.base_url,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
    return client


def sse(events):
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"


def chunk(delta, finish=None, usage=None):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        "usage": usage,
    }


async def test_stream_passes_model_and_collects_text_usage_without_tools_parameter(tmp_path):
    requests = []
    wire = sse(
        [
            chunk({"role": "assistant", "content": ""}),
            chunk({"content": "你"}),
            chunk({"content": "好"}),
            chunk({}, finish="stop"),
            chunk(
                {},
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            ),
        ]
    )

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/chat/completions"
        assert body["model"] == "provider/custom-Model:2026"
        assert body["messages"] == [{"role": "user", "content": "hello"}]
        assert body["stream"] is True
        assert body["enable_thinking"] is False
        assert body["max_tokens"] == 1200
        assert request.headers["authorization"] == "Bearer fake-key"
        assert "tools" not in body
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    client = await mock_client(tmp_path, handler)
    chunks = []
    async with client:
        reply = await SingleRequestRuntime(client).execute(
            "provider/custom-Model:2026",
            [{"role": "user", "content": "hello"}],
            chunks.append,
        )
    assert len(requests) == 1
    assert reply.content == "你好" and chunks == ["你", "好"]
    assert reply.status == "completed" and reply.tool_calls == []
    assert reply.usage.total_tokens == 10
    assert reply.usage.cost == pytest.approx(0.000013)
    assert client.openai.is_closed()


async def test_stream_retains_tool_call_for_runtime_rejection(tmp_path):
    requests = []
    wire = sse(
        [
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":'},
                        }
                    ]
                }
            ),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"a.txt"}'}}]}),
            chunk({}, finish="tool_calls"),
            chunk(
                {},
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            ),
        ]
    )

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    client = await mock_client(tmp_path, handler)
    async with client:
        with pytest.raises(RuntimeResponseError, match="不支持工具响应") as error:
            await SingleRequestRuntime(client).execute("test", [], lambda text: None)
    reply = error.value.reply
    assert reply.tool_calls[0]["arguments"] == '{"path":"a.txt"}'
    assert reply.usage.total_tokens == 10 and len(requests) == 1


async def test_stream_passes_mcp_tool_schemas(tmp_path):
    tools = [
        {
            "name": "mcp_echo_echo",
            "description": "返回文本",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }
    ]
    wire = sse([chunk({"content": "工具已装配"}, finish="stop")])

    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert body["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "mcp_echo_echo",
                    "description": "返回文本",
                    "parameters": tools[0]["parameters"],
                },
            }
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    client = await mock_client(tmp_path, handler)
    async with client:
        reply = await client.stream("test", [], lambda text: None, tools=tools)
    assert reply.content == "工具已装配"
    assert reply.tool_calls == []


async def test_responses_protocol_uses_responses_endpoint_and_usage(tmp_path):
    wire = sse(
        [
            {"type": "response.output_text.delta", "delta": "Hello"},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [],
                    "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
                },
            },
        ]
    )

    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert body["input"] == [{"role": "user", "content": "hello"}]
        assert body["max_output_tokens"] == 1200
        assert request.headers["authorization"] == "Bearer fake-key"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    client = await mock_client(tmp_path, handler, protocol="responses")
    async with client:
        reply = await client.stream(
            "test-model", [{"role": "user", "content": "hello"}], lambda text: None
        )
    assert reply.content == "Hello"
    assert reply.status == "completed"
    assert reply.usage.total_tokens == 6


async def test_anthropic_protocol_uses_messages_endpoint_and_usage(tmp_path):
    wire = sse(
        [
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 5, "output_tokens": 0}},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "你好"},
            },
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 2},
            },
            {"type": "message_stop"},
        ]
    )

    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "fake-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert body["system"] == "角色"
        assert body["messages"] == [{"role": "user", "content": "hello"}]
        assert body["max_tokens"] == 1200
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    client = await mock_client(tmp_path, handler, protocol="anthropic")
    async with client:
        reply = await client.stream(
            "test-model",
            [{"role": "system", "content": "角色"}, {"role": "user", "content": "hello"}],
            lambda text: None,
        )
    assert reply.content == "你好"
    assert reply.status == "completed"
    assert reply.usage.total_tokens == 7


async def test_anthropic_protocol_lists_models_with_api_key_header(tmp_path):
    def handler(request):
        assert request.url.path == "/v1/models"
        assert request.headers["x-api-key"] == "fake-key"
        return httpx.Response(200, json={"data": [{"id": "claude-test"}]})

    client = await mock_client(tmp_path, handler, protocol="anthropic")
    async with client:
        assert await client.list_models() == ["claude-test"]


async def test_premature_stream_end_is_rejected(tmp_path):
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse([chunk({"content": "部分输出"})]),
        )

    client = await mock_client(tmp_path, handler)
    async with client:
        with pytest.raises(RuntimeResponseError, match="未返回完整结果"):
            await SingleRequestRuntime(client).execute("test", [], lambda text: None)


@pytest.mark.parametrize("status", ["length", "content_filter"])
async def test_terminal_failure_keeps_usage_and_partial_text(tmp_path, status):
    wire = sse(
        [
            chunk({"content": "部分输出"}),
            chunk({}, finish=status),
            chunk(
                {},
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            ),
        ]
    )

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    client = await mock_client(tmp_path, handler)
    async with client:
        with pytest.raises(RuntimeResponseError) as error:
            await SingleRequestRuntime(client).execute("test", [], lambda text: None)
    assert error.value.reply.status == status
    assert error.value.reply.content == "部分输出"
    assert error.value.reply.usage.total_tokens == 10


async def test_list_models_preserves_remote_ids(tmp_path):
    def handler(request):
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "vendor/z-model", "object": "model", "created": 0, "owned_by": "vendor"},
                    {"id": "other:model", "object": "model", "created": 0, "owned_by": "other"},
                ],
            },
        )

    client = await mock_client(tmp_path, handler)
    async with client:
        assert await client.list_models() == ["other:model", "vendor/z-model"]


async def test_provider_error_is_redacted_and_never_retried(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            500, json={"error": {"message": "invalid fake-key", "type": "server"}}
        )

    client = await mock_client(tmp_path, handler)
    async with client:
        with pytest.raises(ValueError) as error:
            await client.stream("test", [], lambda text: None)
    assert len(requests) == 1
    assert "fake-key" not in str(error.value) and "[密钥已隐藏]" in str(error.value)


@pytest.mark.parametrize(
    "values",
    [
        {"base_url": ""},
        {"base_url": "example.com/v1"},
        {"base_url": "ftp://example.com/v1"},
        {"base_url": "https:///v1"},
        {"base_url": "http://"},
        {"api_key": ""},
        {"api_key": "  "},
        {"model": ""},
        {"model": "  "},
    ],
)
def test_worker_connection_requires_http_url_key_and_model(values):
    worker = Worker(
        **{
            "id": "worker",
            "name": "测试",
            "model": "custom-model",
            "base_url": "https://example.com/v1",
            "api_key": "fake-key",
            **values,
        }
    )
    with pytest.raises(ValueError):
        worker.require_api()


@pytest.mark.parametrize("base_url", ["http://localhost:8000/v1", "https://example.com/v1"])
def test_worker_accepts_openai_compatible_http_addresses(base_url):
    Worker("worker", "测试", "custom-model", base_url=base_url, api_key="fake-key").require_api()


def test_worker_protocol_is_validated():
    worker = Worker(
        "worker",
        "测试",
        "custom-model",
        base_url="https://example.com/v1",
        api_key="fake-key",
        protocol="unknown",
    )
    with pytest.raises(ValueError, match="协议"):
        worker.require_api()


async def test_list_models_error_redacts_worker_key(tmp_path):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "invalid fake-key"}})

    client = await mock_client(tmp_path, handler)
    async with client:
        with pytest.raises(ValueError) as error:
            await client.list_models()
    assert "fake-key" not in str(error.value)
    assert "[密钥已隐藏]" in str(error.value)


@pytest.mark.parametrize(
    "reply",
    [
        Reply("partial", usage=Usage(3, 2), status="incomplete"),
        Reply("partial", status="failed"),
        Reply("partial"),
        Reply("", status="completed"),
        Reply(" ", status="completed"),
        Reply("tool", tool_calls=[{"id": "call"}], status="completed"),
    ],
)
async def test_runtime_rejects_invalid_response_after_exactly_one_request(reply):
    client = AsyncMock()
    client.stream.return_value = reply
    messages = [{"role": "user", "content": "task"}]

    def on_text(text):
        pass

    with pytest.raises(RuntimeResponseError) as error:
        await SingleRequestRuntime(client).execute("any-model", messages, on_text)
    assert error.value.reply is reply
    client.stream.assert_awaited_once_with("any-model", messages, on_text)


async def test_runtime_returns_complete_reply_unchanged():
    reply = Reply("result", usage=Usage(1, 2), status="completed")
    client = AsyncMock()
    client.stream.return_value = reply
    assert await SingleRequestRuntime(client).execute("any-model", [], lambda text: None) is reply
    assert client.stream.await_count == 1


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
async def test_append_tool_round_uses_protocol_specific_messages(tmp_path, protocol):
    settings = Settings(
        tmp_path / "config.json",
        tmp_path / "data",
        tmp_path,
    )
    worker = Worker(
        "worker",
        "测试",
        "test-model",
        base_url="https://example.com/v1",
        api_key="fake-key",
        protocol=protocol,
    )
    client = ModelClient(settings, worker)
    messages = [{"role": "user", "content": "调用工具"}]
    call = {"id": "call-1", "name": "echo", "arguments": '{"text":"hello"}'}
    reply = Reply(tool_calls=[call], status="tool_calls")
    try:
        client.append_tool_round(messages, reply, [(call, "结果")])
    finally:
        await client.__aexit__()
    if protocol == "chat":
        assert messages[-2]["tool_calls"][0]["function"]["name"] == "echo"
        assert messages[-1] == {"role": "tool", "tool_call_id": "call-1", "content": "结果"}
    elif protocol == "responses":
        assert messages[-2]["type"] == "function_call"
        assert messages[-1] == {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "结果",
        }
    else:
        assert messages[-2]["content"][0]["type"] == "tool_use"
        assert messages[-1]["content"][0]["tool_use_id"] == "call-1"
