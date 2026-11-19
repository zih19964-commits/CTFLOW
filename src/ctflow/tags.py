"""CTflow 结构化上下文传导标签协议（XML 闭合标签隔离角色、技能、预算与上游输出）。"""

from __future__ import annotations

from xml.sax.saxutils import quoteattr

from ctflow.models import Node, Worker


def format_system_context(
    worker: Worker,
    skills: dict[str, str],
    *,
    max_tool_rounds: int | None = None,
) -> str:
    """组装节点的 System 标签上下文：角色定义、挂载技能与工具轮数预算。"""
    blocks: list[str] = []
    if worker.prompt.strip():
        blocks.append(
            f"<worker_role id={quoteattr(worker.id)} name={quoteattr(worker.name)}>\n"
            f"{worker.prompt.strip()}\n"
            f"</worker_role>"
        )
    if skills:
        skill_items = [
            f"<skill id={quoteattr(name)}>\n{body.strip()}\n</skill>"
            for name, body in skills.items()
            if body.strip()
        ]
        if skill_items:
            blocks.append("<mounted_skills>\n" + "\n\n".join(skill_items) + "\n</mounted_skills>")
    if max_tool_rounds is not None:
        blocks.append(
            f'<execution_budget max_tool_rounds="{max_tool_rounds}">\n'
            f"你最多可进行 {max_tool_rounds} 轮工具调用（同一轮可并行调用多个工具）。"
            f"搜集到关键依据后请及时收敛，直接输出最终结论。\n"
            f"</execution_budget>"
        )
    return "\n\n".join(blocks)


def format_workflow_input(
    *,
    workflow_id: str,
    workflow_name: str,
    global_task: str,
    node: Node,
    layer: int,
    upstreams: list[tuple[Node, str]],
) -> str:
    """组装工作流节点的 User 传导标签包：总任务、当前节点职责及直接上游结论。"""
    if upstreams:
        upstream_items = [
            f"<upstream id={quoteattr(up_node.id)} label={quoteattr(up_node.label)} "
            f"worker={quoteattr('@' + up_node.worker_id)}>\n"
            f"{up_output.strip()}\n"
            f"</upstream>"
            for up_node, up_output in upstreams
        ]
        upstream_block = (
            f'<upstream_outputs count="{len(upstreams)}">\n'
            + "\n\n".join(upstream_items)
            + "\n</upstream_outputs>"
        )
    else:
        upstream_block = '<upstream_outputs count="0" />'

    return (
        f"<workflow_input workflow_id={quoteattr(workflow_id)} "
        f"workflow_name={quoteattr(workflow_name)}>\n"
        f"<global_task>\n{global_task.strip()}\n</global_task>\n\n"
        f'<current_node id={quoteattr(node.id)} label={quoteattr(node.label)} layer="{layer}">\n'
        f"{node.task.strip()}\n"
        f"</current_node>\n\n"
        f"{upstream_block}\n"
        f"</workflow_input>"
    )


def format_budget_warning(remaining_rounds: int) -> str:
    return (
        f'<budget_warning remaining_rounds="{remaining_rounds}">'
        f"注意：仅剩最后 {remaining_rounds} 轮工具调用机会，请仅补查核心缺口或直接输出最终结论。"
        f"</budget_warning>"
    )


def format_budget_exhausted(used_rounds: int) -> str:
    return (
        f'<budget_exhausted used_rounds="{used_rounds}">'
        f"工具调用配额（{used_rounds}/{used_rounds} 轮）已全部用尽，工具权限已关闭。"
        f"请立即基于目前已获取的全部信息输出完整最终结论，未核实处如实说明。"
        f"</budget_exhausted>"
    )
