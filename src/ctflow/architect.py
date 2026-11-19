"""独立物理隔离的内置编排服务者 @architect：写死项目规范、窄白名单 MCP 与写前自动快照。"""

from __future__ import annotations

import shutil

from ctflow.config import Settings
from ctflow.mcp_tools import MCPTools
from ctflow.models import ARCHITECT_ID, Worker
from ctflow.secrets import SecretBox
from ctflow.storage import Store
from ctflow.tools import discover_skills

ARCHITECT_SERVER_KEY = "architect_fs"

ARCHITECT_SYSTEM_SPEC = """\
你是 CTflow 的内置编排服务者（@architect），负责根据用户需求创建或优化 Worker 角色与 DAG 工作流。

### 1. CTflow 核心概念与约束
- **Worker**：可复用的角色方案，保存在 `workers/worker-<id>.json`（ID 限字母、数字、下划线或连字符，
  禁止使用保留名 `architect`）。API Key 由系统凭据库保管，JSON 文件内严禁出现 `api_key` 字段。
- **Node**：工作流中的任务实例，包含 `id`（如 `A`、`B`）、`label`（简短标题）、`worker_id`
  （引用已有 Worker ID，不带 `@`）、`task`（节点补充任务）与 `dependencies`（直接上游列表）。
- **Workflow**：由多个 Node 组成的有向无环图（DAG），保存在 `workflows/workflow-<id>.json`。
  严禁自环或循环依赖；每次运行各节点上下文独立，仅接收直接上游节点的最终结论。

### 2. 纯文件数据契约
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
  "source": "flowchart TD\\n    A[\\"调研 @research\\"] --> B[\\"裁决 @judge\\"]"
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
    """为 @architect 挂载窄白名单 filesystem MCP，并在写操作前自动备份快照。"""

    def __init__(self, settings: Settings, store: Store, worker: Worker) -> None:
        self.settings = settings
        self.store = store
        self.worker = worker
        self.schemas: list[dict] = []
        self._mcp: MCPTools | None = None
        self._backed_up = False

    async def __aenter__(self) -> ArchitectTools:
        if not self.worker.mcp_servers:
            return self
        workers_dir = (self.store.data_dir / "workers").resolve()
        workflows_dir = (self.store.data_dir / "workflows").resolve()
        workers_dir.mkdir(parents=True, exist_ok=True)
        workflows_dir.mkdir(parents=True, exist_ok=True)
        extra = {
            ARCHITECT_SERVER_KEY: {
                "transport": "stdio",
                "command": "npx",
                "args": [
                    "-y",
                    "@modelcontextprotocol/server-filesystem",
                    str(workers_dir),
                    str(workflows_dir),
                ],
            }
        }
        self._mcp = MCPTools(self.settings, self.worker, extra_servers=extra)
        await self._mcp.__aenter__()
        self.schemas = list(self._mcp.schemas)
        return self

    async def call(self, name: str, arguments: dict) -> str:
        if self._mcp is None:
            raise ValueError("编排助手未启用文件系统 MCP")
        # 首次调用任何写/改/建目录类工具前自动创建隔离快照，确保一键 /rollback 可恢复。
        lowered = name.lower()
        if not self._backed_up and any(
            kw in lowered for kw in ("write", "edit", "move", "create", "delete")
        ):
            self.store.create_backup()
            self._backed_up = True
        return await self._mcp.call(name, arguments)

    async def __aexit__(self, *exc: object) -> None:
        if self._mcp is not None:
            await self._mcp.__aexit__(*exc)
