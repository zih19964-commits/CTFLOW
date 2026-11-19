"""项目共用的领域数据类：Worker、Node、Workflow 与 Run。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

ARCHITECT_ID = "architect"


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return uuid4().hex[:12]


def upstream_signature(base_url: str) -> str:
    """归一化上游接口根地址，用于识别 Worker 是否更换了上游服务。"""
    raw = base_url.strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/")
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"
    return raw.rstrip("/").lower()


def upstream_label(base_url: str) -> str:
    """提取适合在列表中展示的简短上游主机名。"""
    raw = base_url.strip()
    if not raw:
        return "未配置上游"
    parsed = urlparse(raw)
    return parsed.netloc or raw


@dataclass
class Worker:
    """可复用的角色装配方案：指定模型连接、固定提示词、外置技能和 MCP 服务器。"""

    id: str
    name: str
    model: str
    prompt: str = ""
    skills: list[str] = field(default_factory=list)
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    extra_body: dict = field(default_factory=dict)
    mcp_servers: list[str] = field(default_factory=list)

    def require_api(self) -> None:
        address = urlparse(self.base_url)
        if address.scheme not in {"http", "https"} or not address.netloc or not address.hostname:
            raise ValueError("Worker 的 base_url 必须是 http 或 https 地址")
        if not self.api_key.strip():
            raise ValueError("请在 Worker 中配置非空 api_key")
        if not self.model.strip():
            raise ValueError("请在 Worker 中配置非空 model")

    def redact(self, text: str) -> str:
        if self.api_key:
            return text.replace(self.api_key, "[密钥已隐藏]")
        return text


@dataclass
class Node:
    """工作流中的一个任务实例；dependencies 保存直接上游节点 ID。"""

    id: str
    label: str
    worker_id: str
    task: str = ""
    dependencies: list[str] = field(default_factory=list)


@dataclass
class Workflow:
    """总任务及其有向无环节点图；节点依赖决定执行顺序。"""

    id: str
    name: str
    task: str
    nodes: list[Node]
    source: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Workflow:
        return cls(
            id=data["id"],
            name=data["name"],
            task=data["task"],
            nodes=[Node(**item) for item in data["nodes"]],
            source=data.get("source", ""),
        )


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        if self.cost is None or other.cost is None:
            self.cost = None
        else:
            self.cost += other.cost


@dataclass
class NodeRun:
    """一个节点在单次运行中的状态、输出、日志与用量。"""

    node_id: str
    status: str = "waiting"
    output: str = ""
    error: str = ""
    usage: Usage = field(default_factory=Usage)
    started_at: str | None = None
    finished_at: str | None = None
    logs: list[dict] = field(default_factory=list)
    attempts: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> NodeRun:
        values = dict(data)
        values["usage"] = Usage(**data["usage"])
        return cls(**values)


@dataclass
class Run:
    """一次运行的工作流与 Worker 快照，以及各节点的执行记录。"""

    id: str
    workflow: Workflow
    workers: dict[str, Worker]
    task: str
    status: str = "pending"
    created_at: str = field(default_factory=now)
    finished_at: str | None = None
    nodes: dict[str, NodeRun] = field(default_factory=dict)
    skill_contexts: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def usage(self) -> Usage:
        total = Usage(cost=0.0)
        for node in self.nodes.values():
            if node.attempts:
                total.add(node.usage)
        return total

    @classmethod
    def from_dict(cls, data: dict) -> Run:
        return cls(
            id=data["id"],
            workflow=Workflow.from_dict(data["workflow"]),
            workers={wid: Worker(**wdata) for wid, wdata in data["workers"].items()},
            task=data["task"],
            status=data.get("status", "pending"),
            created_at=data.get("created_at", now()),
            finished_at=data.get("finished_at"),
            nodes={nid: NodeRun.from_dict(ndata) for nid, ndata in data["nodes"].items()},
            skill_contexts={wid: dict(val) for wid, val in data.get("skill_contexts", {}).items()},
        )


def to_dict(value: Worker | Workflow | Run) -> dict:
    """序列化为纯字典；排除 api_key 字段并对所有文本值脱敏密钥。"""
    workers = list(value.workers.values()) if isinstance(value, Run) else [value]
    secrets = sorted(
        {worker.api_key for worker in workers if isinstance(worker, Worker) and worker.api_key},
        key=len,
        reverse=True,
    )

    def redact(item: object) -> object:
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[密钥已隐藏]")
        elif isinstance(item, dict):
            item = {redact(key): redact(part) for key, part in item.items()}
        elif isinstance(item, list):
            item = [redact(part) for part in item]
        return item

    return redact(  # type: ignore[return-value]
        asdict(
            value,
            dict_factory=lambda items: {key: part for key, part in items if key != "api_key"},
        )
    )
