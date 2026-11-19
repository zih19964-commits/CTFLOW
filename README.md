# CTflow

CTflow 是基于 Python、Textual、Rich、MCP SDK 与 `keyring` 构建的本地多 Worker 依赖图终端工作台。每个 Worker 独立装配 OpenAI 兼容接口地址、API Key、模型、附加参数、固定提示词、外置技能与标准 MCP 服务；Node 绑定 Worker 并声明节点任务与直接上游依赖；Workflow 将多个节点组织为有向无环图（DAG），支持顺序推进、并发探索、阶段汇合与独立裁决。

系统内置物理隔离的编排服务者 `@architect`，可根据自然语言需求创建或调整角色与工作流拓扑，并在写入前自动备份配置快照以支持 `/rollback` 一键还原。命令行同时支持跨目录单行调用 `ctflow @<workflow_id> "任务"`，将中间进展输出至 `stderr`，将终点节点的最终结论纯净输出至 `stdout`。

### 安装与启动

首次安装使用 Python 3.11 或更高版本：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\ctflow.exe init
```

启动全屏 FLUX 工作台（也可直接执行根目录 `./start.ps1`）：

```powershell
.\.venv\Scripts\ctflow.exe
```

全局运行参数与 `@architect` 模型连接保存在 `config/config.json`。外置扩展能力集中在 `extensions/` 目录：`extensions/mcp.json` 采用标准 `mcpServers` 结构登记工具服务（已预置 `deepseek-search-fetch-mcp` 联网检索模板），`extensions/skills/<name>/SKILL.md` 存放可勾选挂载的技能文档。业务资产以纯文件 JSON 保存在 `data/workers/`、`data/workflows/` 与 `data/runs/`，所有 API Key 均通过 `keyring` 独立托管在操作系统原生凭据管理器中。

### 工作台交互与命令

工作台包含选择页（Hub）、DAG 拓扑工作台与内存态单 Worker 对话页，支持空输入框按 `←` 返回选择页、点击画布节点直接装配 Worker 与依赖、在配置窗通过空格多选框勾选 MCP 与技能，以及按 `Ctrl+S` 保存。

| 命令 | 用途 |
| --- | --- |
| `/hub` | 返回 Workflows / Workers / Runs 选择页 |
| `/chat`、`/chat <worker-id>` | 进入单 Worker 内存多轮对话（`/chat architect` 打开内置编排助手） |
| `/flow apply [名称]` | 在单聊模式提取最后一轮回复中的 `mermaid` 代码块并生成工作流 |
| `/rollback` | 从 `config/backups/` 最新隔离快照恢复 `workers` 与 `workflows` |
| `/copy` | 复制单聊最后回答或工作流终点节点结果 |
| `/clear`、`/new` | 清空当前单聊内存上下文并开启新对话 |
| `/worker`、`/worker new`、`/worker <id>` | 选择、新建或编辑 Worker 连接、MCP 与技能 |
| `/models`、`/models <worker-id>` | 刷新并直接切换指定 Worker 的模型 |
| `/flow`、`/flow new`、`/flow edit`、`/flow save [新名]` | 加载、可视化新建、编辑或另存工作流 |
| `/node <id>`、`/node new` | 编辑指定节点或新增节点 |
| `/run`、`/run stop` | 启动或停止当前工作流与单聊生成 |
| `/view flow`、`/view run`、`/view result` | 切换编排说明、实时流式与工具轨迹、终点汇总视图 |
| `/export` | 将工作流运行结果导出至 `data/exports/` Markdown 文件 |

### 命令行单行直调与验证

`ctflow` 会自动回退定位安装根目录的 `config/config.json`，支持在任意项目目录或外部智能代理中单行调用：

```powershell
E:\CTflow\.venv\Scripts\ctflow.exe @review "评估当前技术方案的可行性与边界条件"
E:\CTflow\.venv\Scripts\ctflow.exe @@judge "独立复核给定论据"
E:\CTflow\.venv\Scripts\ctflow.exe list workflows
E:\CTflow\.venv\Scripts\ctflow.exe list runs
E:\CTflow\.venv\Scripts\ctflow.exe show <run-id>
E:\CTflow\.venv\Scripts\ctflow.exe mcp <worker-id>
E:\CTflow\.venv\Scripts\ctflow.exe doctor --worker judge --live
E:\CTflow\.venv\Scripts\ctflow.exe rollback
```

自动化检查与测试命令：

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m ruff format --check src tests
```

详细操作说明见 [入门指南](docs/入门指南.md)，内部数据流与标签协议见 [实现说明](docs/实现说明.md)，能力边界见 [实现范围](docs/实现范围.md)，核心架构演进依据见 [ADR 目录](docs/adr/0001-pure-file-storage-and-keyring.md)。
