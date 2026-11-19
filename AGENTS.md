# Repository Guidelines

## 项目结构与产品边界

本仓库交付基于可复用 Worker 与有向无环依赖图（DAG）的本地终端编排工作台，逐项范围见 `docs/实现范围.md`，架构决策记录见 `docs/adr/`，`数据结构课程设计-产品文档.md` 描述完整产品蓝图。技术栈为 Python、Textual、Rich、OpenAI SDK、MCP SDK 与 `keyring`，CLI 与全屏 TUI 共用执行引擎。入门步骤见 `README.md` 与 `docs/入门指南.md`。

项目采用三区物理分离布局：

- `src/ctflow/`：核心应用代码，按 Textual 界面、DAG 拓扑解析与并发调度、闭合 XML 上下文传导、内置隔离编排服务者 `@architect`、标准 MCP 会话与纯文件持久化划分职责。
- `extensions/`：外置生态扩展目录。`extensions/mcp.json` 采用标准 `mcpServers` 格式登记 `stdio` 与 `http` 服务（由 Git 忽略，公开模板见 `extensions/mcp.example.json`），`extensions/skills/<name>/SKILL.md` 保存外置角色技能，`extensions/mcp/` 保存本地 MCP 脚本。
- `config/`：`config/config.json` 保存全局并发、超时、工具轮数上限与 `@architect` 模型连接（由 Git 忽略，公开示例见 `config/config.example.json`），`config/backups/` 保存隔离快照以供一键回滚。
- `data/`：`workers/worker-<id>.json`、`workflows/workflow-<id>.json` 与 `runs/run-<id>.json` 纯文件记录，由 Git 忽略；全部 API Key 通过 `keyring` 保存在操作系统原生凭据管理器中。
- `assets/presets/`：外部静态示例角色与工作流预设 JSON。
- `.agents/skills/ctflow/`：供外部智能代理通过单行命令跨目录调用 CTflow 的操作技能定义。
- `tests/`：单元测试与集成测试，通过内存级 `KeyringBackend` 与可控客户端隔离外部环境。
- `docs/`：入门指南、实现说明与 `docs/adr/` 架构决策记录。

保持领域术语一致：Worker 是可复用的角色装配方案，Node 是引用 Worker 的任务实例，Workflow 是节点及直接依赖组成的执行图。每次节点运行使用独立上下文，通过闭合 XML 标签接收直接上游的最终结论；单 Worker 对话仅驻留内存。

## 构建、测试与本地开发

使用 `uv` 管理依赖与虚拟环境，要求 Python 3.11 或更高版本（开发版本锚点见 `.python-version`）。在项目根目录运行 `uv sync` 创建或同步 `.venv` 并安装开发依赖，依赖变更须同步更新并提交 `uv.lock`。通过 `uv run ctflow init` 初始化配置、扩展目录与示例预设，直接运行 `uv run ctflow` 打开工作台，或在任意目录运行 `uv run --project E:\CTflow ctflow "@review" "评审任务"` 单行调用工作流。

开发依赖配置了 pytest、pytest-asyncio 与 Ruff：

- `uv run pytest`：运行测试套件。
- `uv run ruff check src tests`：检查代码风格与静态问题。
- `uv run ruff format --check src tests`：检查代码格式。

## 代码风格与命名

Python 使用四空格缩进，为公共接口添加类型注解。模块、函数和变量采用 `snake_case`，类采用 `PascalCase`，常量采用 `UPPER_SNAKE_CASE`。关键调度逻辑与安全隔离边界添加简体中文注释。保持界面、执行引擎与纯文件存储分离，模型请求及 MCP 工具调用采用异步方式。按当前数据契约实现功能，保持纯净代码。

## 测试与验收

测试文件命名为 `test_*.py`，测试函数命名为 `test_<行为>`。覆盖拓扑环路检测、并行与汇合、停止、失败阻断、闭合 XML 标签传导、同轮并行工具调用与三级配额收敛、纯文件持久化与坏文件隔离、快照回滚、`keyring` 凭据隔离、`@architect` 物理隔离及 Textual 交互。明确记录已通过、失败及未运行的检查。

## 提交与 Pull Request

提交信息采用 `feat:`、`fix:`、`test:`、`docs:` 等规范前缀，每次提交围绕一个完整变更，分支合并使用 `--no-ff` 保留合并节点。交流使用简体中文。删除文件或目录、批量修改或更新核心依赖前，说明具体目标、影响与后果，并取得明确确认。
