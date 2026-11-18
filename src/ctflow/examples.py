"""可复用的方案评审与调查预设。"""

from copy import deepcopy
from dataclasses import replace

from ctflow.graph import validate_workflow
from ctflow.models import Node, Worker, Workflow
from ctflow.storage import Store


def single_worker(model: str) -> Worker:
    """供 CLI 构造单 Worker 任务。"""
    return Worker("single", "单个 Worker", model, "根据任务给出简洁、明确且有依据的回答。")


def seed_flow_examples(store: Store, template: Worker) -> None:
    """追加三套流程；先检查所有冲突与引用，再写入各自独立的连接配置。"""
    roles = [
        (
            "scout-facts",
            "事实检索",
            "如果服务支持联网检索，搜索议题的关键事实，优先采用原始材料和官方来源。"
            "列出主张、证据链接、发布或更新日期及适用范围，区分事实、推断与未知。"
            "若服务未执行检索，必须如实说明；不得编造来源或声称完成了未经执行的检索。",
        ),
        (
            "scout-practice",
            "实践检索",
            "如果服务支持联网检索，搜索可比较的方案、实施案例和实践结果。"
            "优先采用官方文档、项目记录和可核实案例，说明方案条件、成本、收益与局限。"
            "给出来源链接和日期；若服务未执行检索，必须如实说明，不得编造案例或来源。",
        ),
        (
            "scout-risks",
            "反例检索",
            "如果服务支持联网检索，搜索失败案例、反例、约束及会推翻方案的证据。"
            "说明风险的触发条件、证据强弱和可缓解程度，并附来源链接和日期。"
            "若服务未执行检索，必须如实说明；不得把假设写成事实或编造来源。",
        ),
        (
            "synthesis",
            "证据整合",
            "整理直接上游的证据，建立主张、支持材料、反证、适用条件和可信度的对应关系。"
            "保留关键来源链接、日期与未检索说明，识别重复材料、冲突和缺口。"
            "推理须说明前提与因果，未知项保持未知，为后续独立判断提供可追踪的依据。",
        ),
        (
            "support",
            "支持视角",
            "从值得推进的条件出发独立评估方案，提出最强且有证据的支持理由。"
            "回应主要反证，保留关键证据引用，给出推进条件、实施边界与验证步骤。"
            "若证据不足以支持推进，明确给出暂不支持或有条件支持的判断，不强行赞成。",
        ),
        (
            "challenge",
            "反对视角",
            "从不应推进的条件出发独立检验方案，寻找关键反证、替代解释和更优选择。"
            "保留关键证据引用，指出停止条件、不可接受的代价与仍需验证的风险。"
            "若反对理由不足或风险可控，应如实说明，不强行反对。",
        ),
    ]
    workers = [
        replace(deepcopy(template), id=key, name=name, prompt=prompt, skills=[])
        for key, name, prompt in roles
    ]

    def debate_nodes() -> list[Node]:
        return [
            Node(
                "A",
                "核实事实",
                "scout-facts",
                "检索总任务中的关键事实、定义和约束，标注来源、日期及未核实主张。",
            ),
            Node(
                "B",
                "调查实践",
                "scout-practice",
                "检索可比较的实施方案和实践案例，说明适用条件、成本与实际结果。",
            ),
            Node(
                "C",
                "寻找反例",
                "scout-risks",
                "检索失败案例与反例，找出足以改变决策的风险、约束和替代方案。",
            ),
            Node(
                "D",
                "整合证据",
                "synthesis",
                "对照三路检索结果，合并同源证据，列出一致事实、相互冲突的主张和证据缺口。"
                "保留关键引用及各路实际检索情况，形成供正反双方共同使用的证据清单。",
                ["A", "B", "C"],
            ),
            Node(
                "E",
                "形成支持判断",
                "support",
                "基于整合证据独立提出支持推进的最强论证，回应反证，说明结论及成立条件。"
                "保留关键引用；证据不足时明确暂不支持，不要求与另一分支得出相反结论。",
                ["D"],
            ),
            Node(
                "F",
                "形成反对判断",
                "challenge",
                "基于同一份整合证据独立提出不宜推进的最强论证，比较替代方案并说明停止条件。"
                "保留关键引用；反证不足时如实说明，不要求与另一分支得出相反结论。",
                ["D"],
            ),
        ]

    workflows = [
        Workflow(
            "serial-111",
            "事实调查 → 推理分析 → 独立复核",
            "评估小型团队采用本地优先任务管理工具的可行性，核实关键事实并说明成立条件。",
            [
                Node(
                    "A",
                    "事实调查",
                    "scout-facts",
                    "实际检索议题的关键事实与约束，附来源和日期，明确未知项及检索执行情况。",
                ),
                Node(
                    "B",
                    "推理分析",
                    "synthesis",
                    "以事实调查为依据分析可行性，区分事实与推断，写出推理前提、反证和结论。"
                    "完整保留关键来源、适用条件和待核实项，供下一节点独立复核。",
                    ["A"],
                ),
                Node(
                    "C",
                    "独立复核",
                    "judge",
                    "独立检查上游分析的证据引用、前提与推理是否支撑结论，寻找遗漏和替代解释。"
                    "给出可采纳、需修正或待验证的判断，说明理由；不得把上游引用视作已独立核验。",
                    ["B"],
                ),
            ],
        ),
        Workflow(
            "debate-312",
            "三路检索 → 证据整合 → 正反判断",
            "评估小型团队是否应采用本地优先任务管理工具，比较推进与不推进的证据和条件。",
            debate_nodes(),
        ),
        Workflow(
            "decision-3121",
            "三路检索 → 整合 → 正反判断 → 综合裁决",
            "决定小型团队是否采用本地优先任务管理工具，并给出决策条件、实施边界和验证步骤。",
            debate_nodes()
            + [
                Node(
                    "G",
                    "独立综合裁决",
                    "judge",
                    "独立比较正反两份判断及其证据，识别双方共识、真正分歧和未经证实的前提。"
                    "按证据强弱作出推进、有条件推进、暂缓或放弃的裁决，列出决策触发条件与验证步骤。"
                    "保留关键引用，证据不足时明确待验证，不因分支名称预设结论。",
                    ["E", "F"],
                ),
            ],
        ),
    ]
    existing_workers = {worker.id: worker for worker in store.list_workers()}
    worker_ids = {key.casefold() for key in existing_workers}
    workflow_ids = {workflow.id.casefold() for workflow in store.list_workflows()}
    conflicts = [f"Worker {worker.id}" for worker in workers if worker.id.casefold() in worker_ids]
    conflicts += [
        f"Workflow {workflow.id}"
        for workflow in workflows
        if workflow.id.casefold() in workflow_ids
    ]
    if conflicts:
        raise ValueError("预设 ID 冲突：" + "、".join(conflicts))
    all_workers = {**existing_workers, **{worker.id: worker for worker in workers}}
    for workflow in workflows:
        validate_workflow(workflow, all_workers)
    for worker in workers:
        store.save_worker(worker)
    for workflow in workflows:
        store.save_workflow(workflow)


def seed_examples(store: Store, model: str) -> None:
    """在空库创建示例；连接留空，用户在 Worker 窗口填写后执行。"""
    if store.list_workers() or store.list_workflows():
        return

    workers = [
        Worker(
            "research",
            "资料研究",
            model,
            "依据任务提供的材料整理关键依据，区分已知事实、假设与待核实项。简洁回答。",
        ),
        Worker(
            "code",
            "技术分析",
            model,
            "分析 Python 与 SQLite 的实现可行性，说明核心数据结构、约束与复杂度。简洁回答。",
        ),
        Worker("pro", "优点分析", model, "依据直接上游结果，概括方案优点及成立条件。简洁回答。"),
        Worker(
            "con", "风险分析", model, "依据直接上游结果，指出主要风险并给出具体改进。简洁回答。"
        ),
        Worker(
            "judge",
            "综合评审",
            model,
            "综合直接上游的优点和风险，给出有依据的结论与建议。简洁回答。",
        ),
    ]
    for worker in workers:
        store.save_worker(worker)

    store.save_workflow(
        Workflow(
            id="review",
            name="课程任务管理器方案评审",
            task="评审用Python和SQLite做本地课程任务管理器的方案，分别给出依据、优点、风险与结论",
            nodes=[
                Node("A", "收集依据", "research", "整理所给材料与方案假设，标记待核实项。"),
                Node("B", "分析实现", "code", "分析技术方案与实现约束。"),
                Node("C", "总结优点", "pro", "给出方案的主要优点。", ["A", "B"]),
                Node("D", "评估风险", "con", "给出风险及改进建议。", ["A", "B"]),
                Node(
                    "E",
                    "形成结论",
                    "judge",
                    "权衡优点与风险，给出最终结论。",
                    ["C", "D"],
                ),
            ],
        )
    )
    seed_flow_examples(store, next(worker for worker in workers if worker.id == "judge"))
