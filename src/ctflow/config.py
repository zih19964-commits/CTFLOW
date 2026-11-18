"""读取项目本地 JSON 全局配置，连接配置由各 Worker 保存。"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

_MCP_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass
class Settings:
    config_path: Path
    data_dir: Path
    workspace: Path
    model: str = "qwen3.8-max"
    models: list[str] = field(default_factory=lambda: ["qwen3.8-max", "qwen3.8-flash"])
    concurrency: int = 3
    timeout: float = 60
    max_tokens: int = 1200
    max_tool_rounds: int = 4
    currency: str = "CNY"
    prices: dict[str, dict[str, float]] = field(default_factory=dict)
    mcp_servers: dict[str, dict] = field(default_factory=dict, repr=False)

    def safe_summary(self) -> str:
        return (
            f"默认模型：{self.model}\n工作目录：{self.workspace}\n数据目录：{self.data_dir}\n"
            f"并发：{self.concurrency}\n超时：{self.timeout} 秒\n"
            f"最大输出 Token：{self.max_tokens}\n币种：{self.currency}"
        )


def load_settings(path: Path | None = None) -> Settings:
    config_path = (path or Path.cwd() / "config" / "config.json").resolve()
    if not config_path.is_file():
        raise ValueError(
            f"缺少配置 {config_path}。请复制 config/config.example.json 为 config/config.json。"
        )
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("配置文件必须是合法的 UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("配置文件的顶层必须是 JSON 对象")
    for name in ("workspace", "data_dir"):
        if not isinstance(raw.get(name, ".."), str):
            raise ValueError(f"{name} 必须是字符串路径")
    folder = config_path.parent
    values = {
        **raw,
        "config_path": config_path,
        "workspace": (folder / raw.get("workspace", "..")).resolve(),
        "data_dir": (folder / raw.get("data_dir", "../data")).resolve(),
    }
    try:
        settings = Settings(**values)
    except TypeError as exc:
        raise ValueError("配置中存在未知字段，请对照 config.example.json") from exc
    if not settings.workspace.is_dir():
        raise ValueError("workspace 必须是已存在的目录")
    for name in ("concurrency", "max_tokens", "max_tool_rounds"):
        value = getattr(settings, name)
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} 必须是正整数")
    if not isinstance(settings.timeout, (int, float)) or settings.timeout <= 0:
        raise ValueError("timeout 必须是正数")
    if not isinstance(settings.model, str) or not settings.model.strip():
        raise ValueError("model 必须是非空模型 ID")
    if not isinstance(settings.models, list) or not all(
        isinstance(model, str) and model.strip() for model in settings.models
    ):
        raise ValueError("models 必须是模型名称列表")
    if settings.model not in settings.models:
        settings.models.insert(0, settings.model)
    _validate_mcp_servers(settings.mcp_servers)
    if not isinstance(settings.prices, dict):
        raise ValueError("prices 必须是 JSON 对象")
    for model, price in settings.prices.items():
        if set(price) != {"input", "output"} or any(
            not isinstance(value, (int, float)) or value < 0 for value in price.values()
        ):
            raise ValueError(f"{model} 的价格须包含每百万 Token 的 input/output 非负单价")
    return settings


def _validate_mcp_servers(servers: object) -> None:
    if not isinstance(servers, dict):
        raise ValueError("mcp_servers 必须是 JSON 对象")
    for name, config in servers.items():
        if not isinstance(name, str) or not _MCP_ID.fullmatch(name):
            raise ValueError(f"MCP 服务器名称无效：{name!r}，请使用字母、数字、下划线或连字符")
        if not isinstance(config, dict):
            raise ValueError(f"MCP 服务器 {name} 的配置必须是 JSON 对象")
        transport = config.get("transport")
        if transport == "stdio":
            if not isinstance(config.get("command"), str) or not config["command"].strip():
                raise ValueError(f"MCP {name} 的 stdio 配置需要非空 command")
            args = config.get("args", [])
            if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                raise ValueError(f"MCP {name} 的 args 必须是字符串列表")
            env = config.get("env")
            if env is not None and (
                not isinstance(env, dict)
                or not all(
                    isinstance(key, str) and isinstance(value, str) for key, value in env.items()
                )
            ):
                raise ValueError(f"MCP {name} 的 env 必须是字符串对象")
        elif transport == "http":
            url = config.get("url", "")
            if not isinstance(url, str):
                raise ValueError(f"MCP {name} 的 http 配置需要合法 url")
            address = urlparse(url)
            if address.scheme not in {"http", "https"} or not address.netloc:
                raise ValueError(f"MCP {name} 的 http 配置需要合法 url")
            headers = config.get("headers")
            if headers is not None and (
                not isinstance(headers, dict)
                or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in headers.items()
                )
            ):
                raise ValueError(f"MCP {name} 的 headers 必须是字符串对象")
        else:
            raise ValueError(f"MCP {name} 的 transport 必须是 stdio 或 http")
