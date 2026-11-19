# ADR 0003：闭合 XML 上下文传导协议、工具预算三级收敛与跨目录直调

日期：2026-11-19
状态：已采纳

### 背景与目标

在多阶段并发与汇合的 DAG 工作流中，上游节点的输出正文往往包含多级标题、列表与代码块，下游节点需要准确识别每份材料的来源节点、角色身份与任务边界。同时，外部工具统一收口至标准 MCP 协议后，检索类节点在多步调用过程中若缺乏配额感知，容易因超出轮数上限而中断整条依赖链。此外，CTflow 需要支持外部终端与第三方智能代理通过单行命令跨目录调用任意工作流并获取纯净结论。

### 架构决策

`src/ctflow/tags.py` 定义了统一的闭合 XML 上下文传导标签协议。在 System 消息中，系统按需生成 `<worker_role>`、`<mounted_skills>`（内含各 `<skill>` 子块）与 `<execution_budget>` 标签；在工作流节点的 User 消息中，系统生成 `<workflow_input>` 根标签，内含 `<global_task>`、标注拓扑层级的 `<current_node>`，以及包裹全部直接上游最终结论的 `<upstream_outputs>`，每个 `<upstream>` 元素显式携带上游节点 ID、节点名称与 `@worker_id` 属性。

所有外部工具能力统一由 `src/ctflow/mcp_tools.py` 按标准 MCP 协议接入。`ToolRuntime` 在同一轮收到多个工具调用请求时通过 `asyncio.gather` 并行执行，并实施三级工具轮数预算控制：

1. 开局注入 `<execution_budget max_tool_rounds="N">`，引导模型合并同轮查询并按需收敛。
2. 当可用工具轮数仅剩最后 1 轮时，在工具结果末尾追加 `<budget_warning remaining_rounds="1">` 预警标签。
3. 当全部 `N` 轮工具配额耗尽且模型仍发起工具调用时，执行完最后一批工具后注入 `<budget_exhausted>` 标签，并关闭工具权限发起最后一次收敛总结请求，确保节点稳定产出完整结论交付下游。

在外部调用接口上，`config.py` 在当前工作目录未找到配置时自动回退定位至 CTflow 安装根目录的 `config/config.json`。`cli.py` 提供 `ctflow @<workflow_id> "任务"` 与 `ctflow @@<worker_id> "任务"` 单行简写，中间状态与用量统计输出至 `stderr`，终点节点的最终结论纯净输出至 `stdout`，配合 `.agents/skills/ctflow/SKILL.md` 供外部智能代理无缝编排调用。

### 影响与权衡

闭合 XML 标签消除了上游正文格式对下游提示词结构的干扰，语义属性让裁判与整合节点能够直接对照各方视角来源。三级预算收敛机制将硬性超限失败转化为受控总结，显著提升了多节点检索工作流的端到端完走率；`stdout` 与 `stderr` 的严格分离使任意自动化脚本和外部代理无需额外协议封装即可通过标准命令行管道消费多模型协作成果。
