# Repository Guidelines

## 项目结构与产品边界

本仓库当前交付最小可用版，逐项范围见 `docs/实现范围.md`；`数据结构课程设计-产品文档.md` 描述完整产品愿景。技术栈为 Python、Textual、Rich、SQLite 与 MCP SDK，CLI 与全屏 TUI 共用执行引擎。入门步骤见 `README.md` 与 `docs/入门指南.md`。

项目采用以下布局：

- `src/ctflow/`：应用代码，按界面、工作流调度、模型与工具接入、持久化划分职责。
- `tests/`：单元测试与集成测试，结构对应源码模块。
- `assets/`：主题、示例工作流等静态资源；示例使用虚构数据。
- `pyproject.toml`：依赖、开发工具与启动入口的统一配置。
- `config/config.example.json`：可公开的配置示例；`config/config.json` 保存应用级配置和 MCP 服务器登记，不含 Worker 密钥，由 Git 忽略。
- `data/`：主数据库 `ctflow.db`、配置导出与运行结果，由 Git 忽略；Worker 的 API Key 以平台加密密文保存（Windows DPAPI，其他平台 Fernet）。
- `docs/`：初学者入门与实现说明。

保持领域术语一致：Worker 是可复用装配方案，Node 是引用 Worker 的任务实例，Workflow 是节点及依赖组成的执行图。每次节点运行使用独立上下文，接收直接上游的最终结果。

## 构建、测试与本地开发

使用 Python 3.11 或更高版本。在项目根目录运行 `python -m venv .venv`，再执行 `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`。通过 `.\.venv\Scripts\ctflow.exe init` 初始化配置与示例，直接运行 `.\.venv\Scripts\ctflow.exe` 打开工作台，或运行 `ctflow run review` 使用 CLI。

开发依赖已配置 pytest、pytest-asyncio 与 Ruff。激活虚拟环境后使用：

- `python -m pytest`：运行测试。
- `python -m ruff check .`：检查代码风格与常见问题。
- `python -m ruff format --check .`：检查格式。

## 代码风格与命名

Python 使用四空格缩进，为公共接口添加类型注解。模块、函数和变量采用 `snake_case`，类采用 `PascalCase`，常量采用 `UPPER_SNAKE_CASE`。关键调度逻辑添加简体中文注释。保持 Textual 界面、执行引擎与存储逻辑分离，模型请求及工具调用采用异步方式，避免阻塞界面。按当前数据契约实现功能，删除失效逻辑，避免兼容分支。

## 测试与验收

测试文件命名为 `test_*.py`，测试函数命名为 `test_<行为>`。优先覆盖依赖解析、并行与汇合、停止、失败阻断、上下文隔离、用量汇总、持久化、密钥加密迁移、Chat Completions 请求格式与 MCP 工具调用。模型与工具调用使用可控替身；SQLite 测试使用临时数据库。

当前未设覆盖率门槛。功能提交应附对应测试，并按五 Worker、三阶段场景验证交互；MCP 改动至少覆盖 stdio 工具发现与调用；界面改动附截图或录屏。明确记录已通过、失败及未运行的检查。

## 提交与 Pull Request

当前目录已初始化 Git，尚无提交历史。建议使用 `feat:`、`fix:`、`test:`、`docs:` 等前缀，例如 `feat: 添加工作流依赖调度`。每次提交围绕一个完整变更。注意需要用--no-ff 合并保留合并节点
交流使用简体中文。删除、批量修改或数据库结构变更前，说明具体目标、影响与后果，并取得明确确认。
