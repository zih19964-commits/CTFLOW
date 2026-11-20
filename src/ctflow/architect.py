"""独立物理隔离的内置编排服务者 @architect：写死项目规范、白名单 MCP 网关守卫与写前自动快照。"""

from __future__ import annotations

import shutil
from pathlib import Path

from ctflow.config import Settings
from ctflow.mcp_tools import MCPTools
from ctflow.models import ARCHITECT_ID, Worker
from ctflow.secrets import SecretBox
from ctflow.storage import Store, write_text_atomic
from ctflow.tools import discover_skills

ARCHITECT_SERVER_KEY = "architect_fs"
ARCHITECT_ALLOWED_TOOLS = frozenset(
    {
        "read_text_file",
        "read_multiple_files",
        "write_file",
        "edit_file",
        "list_directory",
    }
)

ARCHITECT_SYSTEM_SPEC = """\
你是 CTflow 的内置编排服务者（@architect），负责根据用户需求创建或优化 Worker 角色与 DAG 工作流。

### 1. CTflow 核心概念与约束
- **Worker**：可复用的角色方案，保存在 `<managed_paths>` 的 `workers/worker-<id>.json`
  （ID 限字母、数字、下划线或连字符，禁止使用保留名 `architect`）。
  API Key 由系统凭据库保管，JSON 文件内严禁出现 `api_key` 字段。
- **Node**：工作流中的任务实例，包含 `id`（如 `A`、`B`）、`label`（简短标题）、`worker_id`
  （引用已有 Worker ID，不带 `@`）、`task`（节点补充任务）与 `dependencies`（直接上游列表）。
- **Workflow**：由多个 Node 组成的有向无环图（DAG），保存在 `<managed_paths>` 的
  `workflows/workflow-<id>.json`。严禁自环或循环依赖；若引用了新角色，请先落盘对应的
  `workers/worker-<id>.json` 再写工作流文件。

### 2. 纯文件数据契约与写入要求
调用 `write_file` 工具时，`path` 请优先填写 `<managed_paths>` 给出的完整目录路径拼接文件名
（如 `<workflows_dir>/workflow-<id>.json`），`content` 参数必须是序列化后的 JSON 字符串，
且文件顶层直接为下述字段（严禁在文件内容外层再包裹 `{"content": ...}`）：

1) `workers/worker-<id>.json` 格式：
```json
{
  "id": "fact-checker",
  "name": "事实核查员",
  "model": "qwen3.8-max",
  "prompt": "角色固定提示词...",
  "skills": [],
  "base_url": "https://...",
  "extra_body": {},
  "mcp_servers": []
}
```
新建 Worker 时，若未指定新接口地址，请复用 `<workspace_inventory>` 中已有可用 Worker 的 `base_url`
与 `model`；`mcp_servers` 与 `skills` 仅可填写清单中已登记的名称。

2) `workflows/workflow-<id>.json` 格式：
```json
{
  "id": "tech-review",
  "name": "技术方案评审",
  "task": "默认总任务描述",
  "nodes": [
    {
      "id": "A",
      "label": "资料调研",
      "worker_id": "research",
      "task": "收集背景依据",
      "dependencies": []
    },
    {
      "id": "B",
      "label": "综合裁决",
      "worker_id": "judge",
      "task": "给出结论与建议",
      "dependencies": ["A"]
    }
  ],
  "source": "flowchart TD\\n    A[\\"资料调研 @research\\"] --> B[\\"综合裁决 @judge\\"]"
}
```

### 3. Mermaid 拓扑语法规范
展示或生成工作流图（填写 `source` 字段或供 `/flow apply` 一键建图）时，必须遵守简化 Mermaid 语法：
- 首行必须是 `flowchart TD` 或 `flowchart LR`；
- 每个节点声明格式：`节点ID["节点名称 @worker_id"]`（引号内末尾必须带空格与 `@worker_id`）；
- 依赖连线格式：`上游ID --> 下游ID`；
- 回答工作流方案时请始终附带标准 ```mermaid 代码块。若已挂载文件系统 MCP 工具且用户要求落盘，
  可直接读写上述受管目录中的 JSON 文件，完成后提示用户可加载运行或通过 `/rollback` 回滚。"""


def build_inventory_xml(settings: Settings, store: Store) -> str:
    """动态构建当前工作区可用资产清单，供 @architect 准确引用已有 ID。"""
    workers_dir = (store.data_dir / "workers").resolve()
    workflows_dir = (store.data_dir / "workflows").resolve()
    workers = store.list_workers()
    workflows = store.list_workflows()
    try:
        skills = sorted(discover_skills(settings.skills_dir).keys())
    except ValueError:
        skills = []
    mcps = sorted(settings.mcp_servers.keys())

    worker_lines = [
        f'  <worker id="{w.id}" name="{w.name}" model="{w.model}" '
        f'base_url="{w.base_url}" mcp="{",".join(w.mcp_servers)}" skills="{",".join(w.skills)}" />'
        for w in workers
    ] or ["  <!-- 当前暂无业务 Worker -->"]

    flow_lines = [
        f'  <workflow id="{wf.id}" name="{wf.name}" nodes="{len(wf.nodes)}" />' for wf in workflows
    ] or ["  <!-- 当前暂无 Workflow -->"]

    return (
        "<workspace_inventory>\n"
        f"<managed_paths workers_dir={workers_dir.as_posix()!r} "
        f"workflows_dir={workflows_dir.as_posix()!r} />\n"
        f"<registered_mcp_servers>{', '.join(mcps) or '无'}</registered_mcp_servers>\n"
        f"<available_skills>{', '.join(skills) or '无'}</available_skills>\n"
        "<existing_workers>\n" + "\n".join(worker_lines) + "\n</existing_workers>\n"
        "<existing_workflows>\n" + "\n".join(flow_lines) + "\n</existing_workflows>\n"
        "</workspace_inventory>"
    )


def get_architect_worker(
    settings: Settings,
    store: Store,
    secrets: SecretBox | None = None,
    *,
    with_inventory: bool = True,
) -> Worker:
    """构造独立隔离的 @architect 实例（不在 data/workers/ 中落盘，提示词只读）。"""
    box = secrets or store._secrets
    arch_cfg = settings.architect
    base_url = str(arch_cfg.get("base_url", "")).strip()
    model = str(arch_cfg.get("model", "")).strip() or settings.model
    api_key = box.get(ARCHITECT_ID)
    extra_body = dict(arch_cfg.get("extra_body", {}))

    prompt = ARCHITECT_SYSTEM_SPEC
    if with_inventory:
        prompt = f"{ARCHITECT_SYSTEM_SPEC}\n\n{build_inventory_xml(settings, store)}"

    has_npx = shutil.which("npx") is not None
    mcp_list = [ARCHITECT_SERVER_KEY] if has_npx else []
    return Worker(
        id=ARCHITECT_ID,
        name=str(arch_cfg.get("name", "编排助手")),
        model=model,
        prompt=prompt,
        skills=[],
        base_url=base_url,
        api_key=api_key,
        extra_body=extra_body,
        mcp_servers=mcp_list,
    )


class ArchitectTools:
    """为 @architect 挂载过滤后的 filesystem MCP，并提供路径归一化、锁内快照与写后契约校验。"""

    def __init__(self, settings: Settings, store: Store, worker: Worker) -> None:
        self.settings = settings
        self.store = store
        self.worker = worker
        self.schemas: list[dict] = []
        self._mcp: MCPTools | None = None
        self._backed_up = False

    def _resolve_managed_path(self, raw_path: str) -> Path:
        """将模型传入的相对路径或绝对路径统一归一化到受管目录，越界立即拦截。"""
        workers_dir = (self.store.data_dir / "workers").resolve()
        workflows_dir = (self.store.data_dir / "workflows").resolve()
        cleaned = raw_path.strip().replace("\\", "/")
        if not cleaned:
            raise ValueError("文件路径不能为空")

        if cleaned in {"workers", "workers/", "data/workers", "data/workers/"}:
            candidate = workers_dir
        elif cleaned in {"workflows", "workflows/", "data/workflows", "data/workflows/"}:
            candidate = workflows_dir
        elif cleaned.startswith("workers/"):
            candidate = (workers_dir / cleaned[len("workers/") :]).resolve()
        elif cleaned.startswith("workflows/"):
            candidate = (workflows_dir / cleaned[len("workflows/") :]).resolve()
        elif cleaned.startswith("data/workers/"):
            candidate = (workers_dir / cleaned[len("data/workers/") :]).resolve()
        elif cleaned.startswith("data/workflows/"):
            candidate = (workflows_dir / cleaned[len("data/workflows/") :]).resolve()
        elif "/" not in cleaned and ":" not in cleaned:
            if cleaned.startswith("workflow-"):
                candidate = (workflows_dir / cleaned).resolve()
            else:
                candidate = (workers_dir / cleaned).resolve()
        else:
            candidate = Path(raw_path.strip()).resolve()

        if not (candidate.is_relative_to(workers_dir) or candidate.is_relative_to(workflows_dir)):
            raise ValueError(
                f"编排助手仅允许访问受管目录 {workers_dir.as_posix()} 与 "
                f"{workflows_dir.as_posix()}，拒绝路径：{raw_path}"
            )
        return candidate

    async def __aenter__(self) -> ArchitectTools:
        if not self.worker.mcp_servers:
            return self
        npx_cmd = shutil.which("npx")
        if not npx_cmd:
            return self
        workers_dir = (self.store.data_dir / "workers").resolve()
        workflows_dir = (self.store.data_dir / "workflows").resolve()
        workers_dir.mkdir(parents=True, exist_ok=True)
        workflows_dir.mkdir(parents=True, exist_ok=True)
        extra = {
            ARCHITECT_SERVER_KEY: {
                "transport": "stdio",
                "command": npx_cmd,
                "args": [
                    "-y",
                    "@modelcontextprotocol/server-filesystem",
                    str(workers_dir),
                    str(workflows_dir),
                ],
            }
        }
        self._mcp = MCPTools(
            self.settings,
            self.worker,
            extra_servers=extra,
            tool_filter=ARCHITECT_ALLOWED_TOOLS,
        )
        await self._mcp.__aenter__()
        self.schemas = list(self._mcp.schemas)
        return self

    async def call(self, name: str, arguments: dict) -> str:
        if self._mcp is None:
            raise ValueError("编排助手未启用文件系统 MCP")
        normalized = dict(arguments)
        target_path: Path | None = None
        if isinstance(normalized.get("path"), str):
            target_path = self._resolve_managed_path(normalized["path"])
            normalized["path"] = str(target_path)
        elif isinstance(normalized.get("paths"), list):
            normalized["paths"] = [
                str(self._resolve_managed_path(str(item))) for item in normalized["paths"]
            ]

        lowered = name.lower()
        is_mutating = any(kw in lowered for kw in ("write", "edit", "move", "create", "delete"))
        if not is_mutating:
            return await self._mcp.call(name, normalized)

        # 写操作全程受跨进程锁保护：首次写前自动备份，写后立即执行 Store 契约校验，不合法当场还原。
        with self.store.write_lock():
            if not self._backed_up:
                self.store.create_backup()
                self._backed_up = True
            existed = target_path is not None and target_path.is_file()
            prior_bytes = target_path.read_bytes() if existed and target_path is not None else None
            result = await self._mcp.call(name, normalized)
            if target_path is not None:
                try:
                    self.store.validate_managed_file(target_path)
                except ValueError as exc:
                    if existed and prior_bytes is not None:
                        write_text_atomic(target_path, prior_bytes.decode("utf-8"))
                    elif target_path.exists():
                        target_path.unlink(missing_ok=True)
                    raise ValueError(
                        f"写入内容未通过 CTflow 数据契约校验（已自动撤销改动）：{exc}"
                    ) from exc
            return result

    async def __aexit__(self, *exc: object) -> None:
        if self._mcp is not None:
            await self._mcp.__aexit__(*exc)
