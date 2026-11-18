# CTflow

CTflow 是一个基于 Python、Textual、Rich 和 SQLite 的终端工作流工作台，正式界面采用 FLUX 布局。每个 Worker 保存独立的接口地址、API Key、模型、附加请求参数、固定上下文、技能和 MCP 服务器；Node 引用 Worker 并定义任务及直接依赖；Workflow 保存一套节点图，可以保留和切换多套。

执行链路为：选择工作流 → 装配 Worker → 加载上下文与技能 → 节点按需调用 MCP 工具 → 展示并保存结果。每个 Worker 可选择 Chat Completions、OpenAI Responses 或 Anthropic Messages 协议，并独立配置自定义服务地址、API Key 和模型 ID。

## 启动

在 PowerShell 中运行：

```powershell
Set-Location E:\CTflow
.\.venv\Scripts\ctflow.exe
```

也可以执行项目根目录的 `./start.ps1`，它会进入项目目录并启动同一个 FLUX 工作台。

首次安装使用 Python 3.11 或更高版本：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\ctflow.exe init
```

在 `/worker <id>` 中为各 Worker 填写协议、`base_url`、API Key、模型和 MCP 服务器。Chat Completions 与 Responses 使用 OpenAI 兼容地址，Anthropic Messages 使用 Anthropic 根地址或 `/v1` 地址；同一工作流可以混用不同供应商和协议。模型 ID 可手填，刷新模型使用当前 Worker 的连接。`config/config.json` 保存并发数、超时、工具轮数和 MCP 服务器登记，以及新 Worker 的默认模型建议。

Worker 的连接保存在本机 `data/ctflow.db`。Windows 上 API Key 使用 DPAPI 加密；其他平台使用 Fernet，并把主密钥保存在 `data/.ctflow.key`。密码框以掩码显示，公开 JSON 导出和持久化运行快照排除 API Key。新 Worker 的接口和密钥留空，需独立填写。备份时同时保留数据库和 `.ctflow.key`；跨平台恢复需要重新填写 Key。

`extra_body` 用于供应商专用参数，其中的值按原文保存；不要把额外令牌或密码放进 `extra_body`。

## 终端操作

点击图中的节点可打开配置并装配 Worker。右侧节点列表单击选择，双击打开配置。底部对话区的空白和提示行可点击进入输入，输入文字与提示文字均可选择、复制，输入光标保持常亮。输入 `/` 显示命令提示，Tab 补全，Enter 执行。配置窗口使用 `Ctrl+S` 保存，`Esc` 或右上角 `×` 取消未保存修改。

Worker 的模型配置和 `/models` 列表都提供“刷新模型”按钮。Worker 窗口使用当前未保存的接口、密钥与请求参数刷新，保留其余表单草稿；模型列表按 Worker 隔离。失败时显示原因，可再次点击重试。

| 命令 | 操作 |
| --- | --- |
| `/worker` | 打开 Worker 列表 |
| `/worker new`、`/worker <id>` | 配置独立接口、API Key、模型、附加参数、固定上下文和技能 |
| `/models`、`/models <worker-id>` | 读取当前节点所引用的 Worker 或指定 Worker 的模型列表 |
| `/flow` | 选择并加载已保存的工作流 |
| `/flow new` | 新建工作流 |
| `/flow edit` | 编辑当前图、名称和任务 |
| `/flow save` | 保存当前工作流 |
| `/flow save 新名称` | 另存一套工作流 |
| `/node A` | 打开指定节点的配置 |
| `/run`、`/run stop` | 执行或停止 |
| `/view flow`、`/view run`、`/view result` | 切换编排、节点输出和结果视图 |
| `/export` | 导出本次运行结果 Markdown |

普通文本更新当前工作流的总任务。编辑保存后，可通过 `/flow` 切换，重启后仍从 SQLite 加载。运行期间可查看节点输出，配置和切换在本次运行结束后进行。

内置工作流包含原三阶段评审、串行 `1→1→1`、三方向检索后正反判断的 `3→1→2`，以及增加综合裁决的 `3→1→2→1`。用途、依赖图和搜索参数见[工作流示例](docs/工作流示例.md)。

## 添加技能

把已有技能复制成 `E:\CTflow\skill\reviewer\SKILL.md`，在 Worker 界面选择 `@reviewer`。打开配置界面时重新扫描目录；技能内容在运行时装入请求。详情见 [技能目录说明](skill/README.md)。

## 接入 MCP

在 `config/config.json` 的 `mcp_servers` 中登记服务器，然后在 Worker 界面的“MCP 服务器”字段填写名称：

```json
{
  "max_tool_rounds": 4,
  "mcp_servers": {
    "echo": {
      "transport": "stdio",
      "command": ".venv/Scripts/python.exe",
      "args": ["tests/fixtures/mcp_server.py"]
    }
  }
}
```

`transport` 支持 `stdio` 和 `http`；HTTP 使用 `url` 与可选 `headers`。Worker 只会看到已登记服务器的工具，模型请求工具时由 MCP 会话执行，再把结果送回模型。`ctflow mcp <worker>` 可只连接并列出该 Worker 的工具，不请求模型。当前最小版聚焦 MCP 工具循环，不提供内置文件/搜索工具，也不实现暂停、失败重试或画布拖拽。

## 命令行与验证

```powershell
.\.venv\Scripts\ctflow.exe doctor
.\.venv\Scripts\ctflow.exe models judge
.\.venv\Scripts\ctflow.exe doctor --worker judge --live
.\.venv\Scripts\ctflow.exe run review
.\.venv\Scripts\ctflow.exe worker judge --task "分析给定方案"
.\.venv\Scripts\ctflow.exe mcp judge
.\.venv\Scripts\ctflow.exe list workflows
.\.venv\Scripts\ctflow.exe list runs
.\.venv\Scripts\ctflow.exe show <run-id>
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m ruff format --check src tests
```

`doctor --worker judge --live` 会实际请求指定 Worker 的模型；`run` 按各 Worker 独立连接执行并产生用量。自动化测试使用可控替身。CLI 成功终点输出写入 stdout，状态与错误写入 stderr。

[入门指南](docs/入门指南.md)介绍配置和操作，[实现说明](docs/实现说明.md)介绍数据对象与执行链路，[实现范围](docs/实现范围.md)列出当前已实现与后续项。选定版原型保留在 [prototypes/tui-terminal](prototypes/tui-terminal/README.md)，正式启动入口使用 `src/ctflow`。

`build/`、`artifacts/` 与 `data/` 是本地构建、验收和运行产物，已由 `.gitignore` 忽略；唯一源码目录是 `src/ctflow`，交付或排查时不要从旧构建目录启动。
