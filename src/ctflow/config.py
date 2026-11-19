"""读取应用全局配置、外置 MCP 注册表与独立隔离的编排助手连接配置。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MCP_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def resolve_config_path(path: Path | None = None) -> Path:
    """优先使用显式路径，其次当前工作目录，最后自动回退至 CTflow 安装根目录以支持跨目录直调。"""
    if path is not None:
        return path.resolve()
    cwd_candidate = Path.cwd() / "config" / "config.json"
    if cwd_candidate.is_file():
        return cwd_candidate.resolve()
    root_candidate = PROJECT_ROOT / "config" / "config.json"
    if root_candidate.is_file():
        return root_candidate.resolve()
    return cwd_candidate.resolve()


def normalize_mcp_servers(servers: object) -> dict[str, dict]:
    """校验并归一化业界标准 mcpServers 配置（按 command / url 自动识别 stdio 与 http）。"""
    if not isinstance(servers, dict):
        raise ValueError("mcpServers 必须是 JSON 对象")
    normalized: dict[str, dict] = {}
    for name, config in servers.items():
        if not isinstance(name, str) or not _MCP_ID.fullmatch(name):
            raise ValueError(f"MCP 服务器名称无效：{name!r}，请使用字母、数字、下划线或连字符")
        if not isinstance(config, dict):
            raise ValueError(f"MCP 服务器 {name} 的配置必须是 JSON 对象")
        transport = config.get("transport")
        if transport is None:
            if "command" in config:
                transport = "stdio"
            elif "url" in config:
                transport = "http"
        if transport == "stdio":
            cmd = config.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                raise ValueError(f"MCP {name} 的 stdio 配置需要非空 command")
            args = config.get("args", [])
            if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                raise ValueError(f"MCP {name} 的 args 必须是字符串列表")
            env = config.get("env")
            if env is not None and (
                not isinstance(env, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
            ):
                raise ValueError(f"MCP {name} 的 env 必须是字符串对象")
            normalized[name] = {
                "transport": "stdio",
                "command": cmd.strip(),
                "args": list(args),
                **({"env": dict(env)} if env is not None else {}),
            }
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
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
            ):
                raise ValueError(f"MCP {name} 的 headers 必须是字符串对象")
            normalized[name] = {
                "transport": "http",
                "url": url,
                **({"headers": dict(headers)} if headers is not None else {}),
            }
        else:
            raise ValueError(f"MCP {name} 需提供有效的 command (stdio) 或 url (http)")
    return normalized


def load_external_mcp_servers(extensions_dir: Path) -> dict[str, dict]:
    """从外置目录 extensions/mcp.json 读取标准 mcpServers 注册表。"""
    mcp_path = extensions_dir / "mcp.json"
    if not mcp_path.is_file():
        return {}
    try:
        raw = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"MCP 配置文件 {mcp_path} 必须是合法的 UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("mcp.json 顶层必须是 JSON 对象")
    servers = raw.get("mcpServers", raw) if "mcpServers" in raw else raw
    return normalize_mcp_servers(servers)


@dataclass
class Settings:
    config_path: Path
    data_dir: Path
    workspace: Path
    extensions_dir: Path | None = None
    backup_dir: Path | None = None
    model: str = "qwen3.8-max"
    models: list[str] = field(default_factory=lambda: ["qwen3.8-max", "qwen3.8-flash"])
    concurrency: int = 3
    timeout: float = 60
    max_tokens: int = 1200
    max_tool_rounds: int = 6
    currency: str = "CNY"
    prices: dict[str, dict[str, float]] = field(default_factory=dict)
    mcp_servers: dict[str, dict] = field(default_factory=dict, repr=False)
    architect: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.config_path = self.config_path.resolve()
        self.data_dir = self.data_dir.resolve()
        self.workspace = self.workspace.resolve()
        if self.extensions_dir is None:
            self.extensions_dir = (self.workspace / "extensions").resolve()
        else:
            self.extensions_dir = self.extensions_dir.resolve()
        if self.backup_dir is None:
            self.backup_dir = (self.config_path.parent / "backups").resolve()
        else:
            self.backup_dir = self.backup_dir.resolve()
        if self.mcp_servers:
            self.mcp_servers = normalize_mcp_servers(self.mcp_servers)

    @property
    def skills_dir(self) -> Path:
        assert self.extensions_dir is not None
        return self.extensions_dir / "skills"

    def safe_summary(self) -> str:
        return (
            f"默认模型：{self.model}\n工作目录：{self.workspace}\n"
            f"扩展目录：{self.extensions_dir}\n数据目录：{self.data_dir}\n"
            f"并发：{self.concurrency}\n超时：{self.timeout} 秒\n"
            f"最大输出 Token：{self.max_tokens}\n工具轮数上限：{self.max_tool_rounds}\n"
            f"币种：{self.currency}"
        )


def load_settings(path: Path | None = None) -> Settings:
    config_path = resolve_config_path(path)
    if not config_path.is_file():
        raise ValueError(
            f"缺少配置 {config_path}。请运行 ctflow init 或复制 config/config.example.json。"
        )
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("配置文件必须是合法的 UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("配置文件的顶层必须是 JSON 对象")

    for name in ("workspace", "data_dir", "extensions_dir"):
        if name in raw and not isinstance(raw[name], str):
            raise ValueError(f"{name} 必须是字符串路径")

    folder = config_path.parent
    workspace = (folder / raw.get("workspace", "..")).resolve()
    data_dir = (folder / raw.get("data_dir", "../data")).resolve()
    default_ext = "../extensions" if folder.name == "config" else "extensions"
    extensions_dir = (folder / raw.get("extensions_dir", default_ext)).resolve()

    inline_mcp = raw.get("mcp_servers")
    clean_raw = {
        k: v
        for k, v in raw.items()
        if k not in {"workspace", "data_dir", "extensions_dir", "mcp_servers"}
    }
    try:
        settings = Settings(
            config_path=config_path,
            data_dir=data_dir,
            workspace=workspace,
            extensions_dir=extensions_dir,
            **clean_raw,
        )
    except TypeError as exc:
        raise ValueError("配置中存在未知字段，请对照 config/config.example.json") from exc

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
        isinstance(m, str) and m.strip() for m in settings.models
    ):
        raise ValueError("models 必须是模型名称列表")
    if settings.model not in settings.models:
        settings.models.insert(0, settings.model)
    if not isinstance(settings.architect, dict):
        raise ValueError("architect 配置必须是 JSON 对象")
    if not isinstance(settings.prices, dict):
        raise ValueError("prices 必须是 JSON 对象")
    for model, price in settings.prices.items():
        if set(price) != {"input", "output"} or any(
            not isinstance(v, (int, float)) or v < 0 for v in price.values()
        ):
            raise ValueError(f"{model} 的价格须包含每百万 Token 的 input/output 非负单价")

    ext_servers = load_external_mcp_servers(extensions_dir)
    if inline_mcp is not None:
        ext_servers = {**ext_servers, **normalize_mcp_servers(inline_mcp)}
    settings.mcp_servers = ext_servers
    return settings


def save_architect_connection(
    settings: Settings, *, model: str, base_url: str, extra_body: dict
) -> None:
    """将独立隔离的 @architect 模型连接写回 config.json（API Key 独立保存在 keyring）。"""
    settings.architect = {
        "name": settings.architect.get("name", "编排助手"),
        "model": model,
        "base_url": base_url,
        "extra_body": extra_body,
    }
    if not settings.config_path.is_file():
        return
    raw = json.loads(settings.config_path.read_text(encoding="utf-8"))
    raw["architect"] = dict(settings.architect)
    settings.config_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
