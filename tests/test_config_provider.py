"""配置自动寻址、外置 mcp.json 标准解析、Chat Completions 流式接口与单次请求 Runtime 测试。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI

from ctflow.config import PROJECT_ROOT, Settings, load_settings, resolve_config_path
from ctflow.models import Usage, Worker
from ctflow.provider import ModelClient, Reply
from ctflow.runtime import RuntimeResponseError, SingleRequestRuntime


def write_config(tmp_path, **values):
    path = tmp_path / "config" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "workspace": "..",
                "data_dir": "../data",
                "extensions_dir": "../extensions",
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
    assert settings.extensions_dir == tmp_path / "extensions"
    assert settings.skills_dir == tmp_path / "extensions" / "skills"
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
    assert "密钥" not in settings.safe_summary()
    async with ModelClient(settings, worker) as client:
        assert client.openai.max_retries == 0
        assert client.openai.timeout == settings.timeout
        assert client.usage("qwen3.8-max", 100000, 100000).cost == pytest.approx(0.3)
        assert client.usage("unknown", 1, 1).cost is None


def test_cross_directory_config_fallback_to_project_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    resolved = resolve_config_path(None)
    if (PROJECT_ROOT / "config" / "config.json").is_file():
        assert resolved == (PROJECT_ROOT / "config" / "config.json").resolve()


def test_external_mcp_json_standard_mcpservers_format(tmp_path):
    path = write_config(tmp_path, max_tool_rounds=5)
    ext_dir = tmp_path / "extensions"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "deepseek_web": {
                        "command": "uvx",
                        "args": ["deepseek-search-fetch-mcp"],
                        "env": {"DEEPSEEK_API_KEY": "sk-test"},
                    },
                    "remote_doc": {
                        "url": "https://mcp.example.com/sse",
                        "headers": {"Authorization": "Bearer tok"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.max_tool_rounds == 5
    assert settings.mcp_servers["deepseek_web"]["transport"] == "stdio"
    assert settings.mcp_servers["remote_doc"]["transport"] == "http"


@pytest.mark.parametrize(
    "invalid",
    [
        {"concurrency": 0},
        {"max_tokens": -1},
        {"typo": 1},
        {"model": " "},
        {"models": [" "]},
        {"max_tool_rounds": 0},
        {"mcp_servers": {"bad name": {"command": "x"}}},
        {"mcp_servers": {"echo": {"transport": "stdio"}}},
        {"mcp_servers": {"echo": {"url": "ftp://example.com"}}},
        {"base_url": "https://example.com/v1"},
        {"api_key": "removed-global-key"},
    ],
)
def test_invalid_and_removed_config_fields_are_reported(tmp_path, invalid):
    with pytest.raises(ValueError):
        load_settings(write_config(tmp_path, **invalid))


async def mock_client(tmp_path, handler):
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
    )
    client = ModelClient(settings, worker)
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
            chunk({}, usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}),
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
        assert body["max_tokens"] == 4096
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
    wire = sse(
        [
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "mcp_echo_echo", "arguments": '{"text":'},
                        }
                    ]
                }
            ),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"hi"}'}}]}),
            chunk({}, finish="tool_calls"),
            chunk({}, usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}),
        ]
    )

    client = await mock_client(
        tmp_path,
        lambda req: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=wire
        ),
    )
    async with client:
        with pytest.raises(RuntimeResponseError, match="不支持工具响应") as error:
            await SingleRequestRuntime(client).execute("test", [], lambda text: None)
    reply = error.value.reply
    assert reply.tool_calls[0]["arguments"] == '{"text":"hi"}'
    assert reply.usage.total_tokens == 10


async def test_stream_passes_mcp_tool_schemas_and_appends_round(tmp_path):
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
        assert body["tools"][0]["function"]["name"] == "mcp_echo_echo"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    client = await mock_client(tmp_path, handler)
    async with client:
        reply = await client.stream("test", [], lambda text: None, tools=tools)
        messages = [{"role": "user", "content": "调用"}]
        call = {"id": "c1", "name": "mcp_echo_echo", "arguments": '{"text":"a"}'}
        client.append_tool_round(messages, Reply(tool_calls=[call]), [(call, "结果")])
    assert reply.content == "工具已装配"
    assert messages[-2]["tool_calls"][0]["function"]["name"] == "mcp_echo_echo"
    assert messages[-1] == {"role": "tool", "tool_call_id": "c1", "content": "结果"}


async def test_provider_error_and_list_models_redact_worker_key(tmp_path):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "invalid fake-key"}})

    client = await mock_client(tmp_path, handler)
    async with client:
        with pytest.raises(ValueError) as err1:
            await client.stream("test", [], lambda text: None)
        with pytest.raises(ValueError) as err2:
            await client.list_models()
    assert "fake-key" not in str(err1.value) and "[密钥已隐藏]" in str(err1.value)
    assert "fake-key" not in str(err2.value) and "[密钥已隐藏]" in str(err2.value)


@pytest.mark.parametrize(
    "reply",
    [
        Reply("partial", usage=Usage(3, 2), status="incomplete"),
        Reply("partial", status="failed"),
        Reply("", status="completed"),
        Reply(" ", status="completed"),
        Reply("tool", tool_calls=[{"id": "call"}], status="completed"),
    ],
)
async def test_runtime_rejects_invalid_response_after_exactly_one_request(reply):
    client = AsyncMock()
    client.stream.return_value = reply
    with pytest.raises(RuntimeResponseError) as error:
        await SingleRequestRuntime(client).execute("any-model", [], lambda text: None)
    assert error.value.reply is reply
    client.stream.assert_awaited_once()
