"""定向加载原型模型，验证调度与装配的公共契约。"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ctflow_terminal_prototype_model", Path(__file__).with_name("model.py")
)
assert _SPEC is not None and _SPEC.loader is not None
_MODEL = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODEL
_SPEC.loader.exec_module(_MODEL)
Demo = _MODEL.Demo
STATES = {"waiting", "running", "completed", "defeat"}


def nodes(demo):
    return {node.id: node for node in demo.nodes}


def snapshot(demo):
    """拒绝编辑时，节点、结果、选择和事件记录应一并保持完整。"""
    return (
        demo.status,
        demo.selected.id,
        demo.elapsed,
        demo.tokens,
        demo.artifact,
        demo.revision,
        repr(demo.messages),
        repr(demo.logs),
        tuple(
            (
                n.id,
                n.name,
                n.worker,
                n.deps,
                n.task,
                n.status,
                n.output,
                n.tokens,
                n.started,
                n.ended,
                n.error,
                n.duration,
            )
            for n in demo.nodes
        ),
    )


def finish(demo):
    demo.tick(1000)
    assert not demo.busy
    assert demo.status in STATES
    assert all(n.status in STATES for n in demo.nodes)


def test_parallel_barrier_requires_every_parent():
    demo = Demo()
    by_id = nodes(demo)
    assert [n.deps for n in demo.nodes] == [(), (), ("A", "B"), ("A", "B"), ("C", "D")]
    for key, duration in {"A": 1, "B": 3, "C": 2, "D": 4, "E": 1}.items():
        by_id[key].duration = duration
    demo.start()
    assert {n.id for n in demo.nodes if n.status == "running"} == {"A", "B"}
    demo.tick(1)
    assert by_id["A"].status == "completed"
    assert by_id["B"].status == "running"
    assert all(by_id[k].status == "waiting" for k in ("C", "D", "E"))
    demo.tick(2)
    assert by_id["C"].started == by_id["D"].started == 3
    demo.tick(2)
    assert by_id["C"].status == "completed"
    assert by_id["D"].status == "running"
    assert by_id["E"].status == "waiting"
    demo.tick(2)
    assert by_id["E"].started == 7
    finish(demo)
    assert demo.elapsed == 8


@pytest.mark.parametrize("scenario", ["normal", "defeat"])
def test_large_and_small_ticks_have_same_event_times_and_results(scenario):
    large, small = Demo(), Demo()
    large.start(scenario)
    small.start(scenario)
    large.tick(100)
    for _ in range(1000):
        small.tick(0.1)
    assert large.status == small.status
    assert large.elapsed == pytest.approx(small.elapsed)
    assert large.tokens == small.tokens
    assert large.artifact == small.artifact
    for left, right in zip(large.nodes, small.nodes, strict=True):
        assert (left.id, left.status, left.output, left.tokens, left.error) == (
            right.id,
            right.status,
            right.output,
            right.tokens,
            right.error,
        )
        assert left.started == pytest.approx(right.started)
        assert left.ended == pytest.approx(right.ended)


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d.assign("A", "writer"),
        lambda d: d.add_node("补充写作", "writer"),
        lambda d: d.set_task("A", "新的任务"),
        lambda d: d.set_dependencies("C", ("A",)),
        lambda d: d.plan("新的方案"),
        lambda d: d.load_flow("serial"),
    ],
)
def test_running_rejects_assembly_without_mutation(edit):
    demo = Demo()
    demo.start()
    before = snapshot(demo)
    with pytest.raises(ValueError):
        edit(demo)
    assert snapshot(demo) == before
    demo.select("B")
    assert demo.selected.id == "B"
    assert demo.status == "running"


def test_assign_uses_preset_and_changes_rerun_output():
    demo = Demo()
    assert set(demo.workers) == {"research", "code", "pro", "con", "judge", "writer"}
    demo.start()
    finish(demo)
    original = nodes(demo)["A"].output
    demo.assign("A", "writer")
    assert nodes(demo)["A"].worker == "writer"
    demo.start()
    finish(demo)
    assert nodes(demo)["A"].output
    assert nodes(demo)["A"].output != original


def test_new_node_task_and_dependencies_take_effect():
    demo = Demo()
    previous_ids = set(nodes(demo))
    demo.add_node("成稿复核", "writer", ("E",))
    added_ids = set(nodes(demo)) - previous_ids
    assert len(added_ids) == 1
    added = nodes(demo)[added_ids.pop()]
    assert (added.name, added.worker, added.deps) == ("成稿复核", "writer", ("E",))
    demo.set_task(added.id, "核对证据并生成简短成稿")
    demo.set_dependencies(added.id, ("C", "D"))
    assert added.task == "核对证据并生成简短成稿"
    demo.start()
    finish(demo)
    assert added.status == "completed"
    assert added.started == max(nodes(demo)[k].ended for k in ("C", "D"))


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d.set_dependencies("A", ("E",)),
        lambda d: d.set_dependencies("A", ("A",)),
        lambda d: d.set_dependencies("C", ("UNKNOWN",)),
        lambda d: d.add_node("无效节点", "writer", ("UNKNOWN",)),
        lambda d: d.add_node("无效预设", "UNKNOWN"),
    ],
)
def test_invalid_graph_edits_do_not_pollute_model(edit):
    demo = Demo()
    before = snapshot(demo)
    with pytest.raises(ValueError):
        edit(demo)
    assert snapshot(demo) == before
    demo.start()
    finish(demo)
    assert demo.status == "completed"


def test_defeat_blocks_descendants_and_allows_parallel_branch_to_finish():
    demo = Demo()
    demo.start("defeat")
    finish(demo)
    by_id = nodes(demo)
    assert demo.status == "defeat"
    assert {by_id[key].status for key in ("C", "D")} == {"completed", "defeat"}
    assert by_id["E"].status == "defeat"
    assert by_id["E"].started is None


def test_stop_settles_every_node_and_freezes_clock():
    demo = Demo()
    demo.start()
    demo.tick(0.25)
    demo.stop()
    assert demo.status == "defeat"
    assert not demo.busy
    assert all(n.status in {"completed", "defeat"} for n in demo.nodes)
    before = snapshot(demo)
    demo.tick(100)
    assert snapshot(demo) == before


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d.assign("A", "writer"),
        lambda d: d.set_task("A", "重新研究"),
        lambda d: d.set_dependencies("C", ("A",)),
        lambda d: d.add_node("成稿", "writer", ("E",)),
    ],
)
def test_assembly_clears_previous_run_results(edit):
    demo = Demo()
    demo.start()
    finish(demo)
    assert demo.artifact and demo.tokens > 0
    edit(demo)
    assert demo.status == "waiting"
    assert demo.elapsed == demo.tokens == demo.complete_count == 0
    assert demo.artifact == ""
    for node in demo.nodes:
        assert node.status == "waiting"
        assert node.output == ""
        assert node.tokens == 0
        assert node.started is node.ended is None
        assert not node.error


def test_token_sum_and_monotonic_time():
    demo = Demo()
    demo.start()
    previous_time = previous_tokens = 0
    for _ in range(200):
        demo.tick(0.1)
        assert demo.elapsed >= previous_time
        assert demo.tokens >= previous_tokens
        assert demo.tokens == sum(n.tokens for n in demo.nodes)
        assert demo.status in STATES
        assert all(n.status in STATES for n in demo.nodes)
        previous_time, previous_tokens = demo.elapsed, demo.tokens
    assert demo.status == "completed"
    for node in demo.nodes:
        assert 0 <= node.started <= node.ended <= demo.elapsed
        assert node.ended - node.started == pytest.approx(node.duration)


def test_all_completed_generates_markdown_artifact():
    demo = Demo()
    demo.plan("比较两个本地笔记工具方案")
    demo.start()
    finish(demo)
    assert demo.status == "completed"
    assert demo.complete_count == len(demo.nodes)
    assert demo.artifact.startswith("# ")
    assert "比较两个本地笔记工具方案" in demo.artifact
    assert all(n.output for n in demo.nodes)


@pytest.mark.parametrize("preset", ["review", "serial", "new"])
def test_flow_presets_have_valid_dependencies_and_waiting_state(preset):
    demo = Demo()
    demo.load_flow(preset)
    by_id = nodes(demo)
    assert demo.status == "waiting"
    assert all(n.status == "waiting" for n in demo.nodes)
    assert all(set(n.deps) <= by_id.keys() for n in demo.nodes)
    if preset == "serial":
        assert [n.deps for n in demo.nodes] == [(), ("A",), ("B",), ("C",), ("D",)]
