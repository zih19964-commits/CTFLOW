"""项目共用的数据类，界面、调度器和 SQLite 使用同一份定义。"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return uuid4().hex[:12]


@dataclass
class Worker:
    """可复用的角色配置：指定模型连接、固定提示词、技能和 MCP 服务器。"""

    id: str
    name: str
    model: str
    prompt: str = ""
    skills: list[str] = field(default_factory=list)
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    extra_body: dict = field(default_factory=dict)
    mcp_servers: list[str] = field(default_factory=list)
    protocol: str = "chat"

    def require_api(self) -> None:
        if self.protocol not in {"chat", "responses", "anthropic"}:
            raise ValueError("Worker 协议必须是 chat、responses 或 anthropic")
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
    """工作流中的一个任务；dependencies 保存直接上游的节点 ID。"""

    id: str
    label: str
    worker_id: str
    task: str = ""
    dependencies: list[str] = field(default_factory=list)


@dataclass
class Workflow:
    """总任务及其节点图；节点之间的依赖决定执行顺序。"""

    id: str
    name: str
    task: str
    nodes: list[Node]
    source: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Workflow":
        values = data.copy()
        nodes = []
        for node_data in data["nodes"]:
            nodes.append(Node(**node_data))
        values["nodes"] = nodes
        return cls(**values)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        if self.cost is None or other.cost is None:
            self.cost = None
        else:
            self.cost += other.cost


@dataclass
class NodeRun:
    """一个节点在某次运行中的状态、输出和用量。"""

    node_id: str
    status: str = "waiting"
    output: str = ""
    error: str = ""
    usage: Usage = field(default_factory=Usage)
    started_at: str | None = None
    finished_at: str | None = None
    logs: list[dict] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    attempts: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "NodeRun":
        values = data.copy()
        values["usage"] = Usage(**data["usage"])
        return cls(**values)


@dataclass
class Run:
    """一次运行的工作流和 Worker 快照，以及各节点的运行记录。"""

    id: str
    workflow: Workflow
    workers: dict[str, Worker]
    task: str
    status: str = "pending"
    created_at: str = field(default_factory=now)
    finished_at: str | None = None
    nodes: dict[str, NodeRun] = field(default_factory=dict)
    skill_contexts: dict[str, str] = field(default_factory=dict)

    @property
    def usage(self) -> Usage:
        total = Usage(cost=0.0)
        for node in self.nodes.values():
            if node.attempts:
                total.add(node.usage)
        return total

    @classmethod
    def from_dict(cls, data: dict) -> "Run":
        values = data.copy()
        values["workflow"] = Workflow.from_dict(data["workflow"])
        workers = {}
        for worker_id, worker_data in data["workers"].items():
            workers[worker_id] = Worker(**worker_data)
        values["workers"] = workers
        nodes = {}
        for node_id, node_data in data["nodes"].items():
            nodes[node_id] = NodeRun.from_dict(node_data)
        values["nodes"] = nodes
        return cls(**values)


def to_dict(value: Worker | Workflow | Run) -> dict:
    # 只有 Worker 表保留凭据；所有公开快照连同文本中的密钥一起脱敏。
    workers = list(value.workers.values()) if isinstance(value, Run) else [value]
    secrets = sorted(
        {worker.api_key for worker in workers if isinstance(worker, Worker) and worker.api_key},
        key=len,
        reverse=True,
    )

    def redact(item):
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[密钥已隐藏]")
        elif isinstance(item, dict):
            item = {redact(key): redact(part) for key, part in item.items()}
        elif isinstance(item, list):
            item = [redact(part) for part in item]
        return item

    return redact(
        asdict(
            value,
            dict_factory=lambda items: dict((key, part) for key, part in items if key != "api_key"),
        )
    )
