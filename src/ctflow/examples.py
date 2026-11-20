"""从外部静态 JSON 资源加载示例 Worker 与多阶段评审工作流预设。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

from ctflow.config import PROJECT_ROOT
from ctflow.graph import to_mermaid, validate_workflow
from ctflow.models import Node, Worker, Workflow
from ctflow.storage import Store

_PRESET_FILE = PROJECT_ROOT / "assets" / "presets" / "default_presets.json"
_FLOW_WORKER_IDS = frozenset(
    {"scout-facts", "scout-practice", "scout-risks", "synthesis", "support", "challenge"}
)
_FLOW_IDS = frozenset({"serial-111", "debate-312", "decision-3121"})


def _load_preset_bundle() -> dict:
    return json.loads(_PRESET_FILE.read_text(encoding="utf-8"))


def seed_flow_examples(store: Store, template: Worker) -> None:
    """以指定 Worker 的连接为模板追加三套辩论/复核工作流及六个检索视角角色。"""
    bundle = _load_preset_bundle()
    workers = [
        replace(
            deepcopy(template),
            id=item["id"],
            name=item["name"],
            prompt=item["prompt"],
            skills=[],
        )
        for item in bundle["workers"]
        if item["id"] in _FLOW_WORKER_IDS
    ]
    workflows = []
    for item in bundle["workflows"]:
        if item["id"] not in _FLOW_IDS:
            continue
        wf = Workflow(
            id=item["id"],
            name=item["name"],
            task=item["task"],
            nodes=[Node(**n) for n in item["nodes"]],
        )
        workflows.append(wf)

    existing_workers = {w.id: w for w in store.list_workers()}
    worker_ids = {k.casefold() for k in existing_workers}
    workflow_ids = {wf.id.casefold() for wf in store.list_workflows()}
    conflicts = [f"Worker {w.id}" for w in workers if w.id.casefold() in worker_ids]
    conflicts += [f"Workflow {wf.id}" for wf in workflows if wf.id.casefold() in workflow_ids]
    if conflicts:
        raise ValueError("预设 ID 冲突：" + "、".join(conflicts))

    all_workers = {**existing_workers, **{w.id: w for w in workers}}
    for wf in workflows:
        validate_workflow(wf, all_workers)
        wf.source = to_mermaid(wf)
    for w in workers:
        store.save_worker(w)
    for wf in workflows:
        store.save_workflow(wf)


def seed_examples(store: Store, model: str) -> None:
    """在空数据目录初始化默认示例角色与工作流；加跨进程写入锁防止多开首次启动竞态。"""
    with store.write_lock():
        if store.list_workers() or store.list_workflows():
            return
        bundle = _load_preset_bundle()
        base_workers = [
            Worker(item["id"], item["name"], model, item["prompt"])
            for item in bundle["workers"]
            if item["id"] not in _FLOW_WORKER_IDS
        ]
        for w in base_workers:
            store.save_worker(w)
        for item in bundle["workflows"]:
            if item["id"] == "review":
                wf = Workflow(
                    id=item["id"],
                    name=item["name"],
                    task=item["task"],
                    nodes=[Node(**n) for n in item["nodes"]],
                )
                wf.source = to_mermaid(wf)
                store.save_workflow(wf)
        judge = next(w for w in base_workers if w.id == "judge")
        seed_flow_examples(store, judge)
