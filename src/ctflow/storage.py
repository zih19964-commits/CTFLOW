"""纯文件持久化存储、跨进程写入锁、乐观并发控制、坏文件隔离与快照回滚。"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from ctflow.graph import topological_layers, validate_workflow
from ctflow.models import ARCHITECT_ID, Run, Worker, Workflow, new_id, to_dict
from ctflow.secrets import SecretBox

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_VALID_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MAX_BACKUPS = 10
_REPLACE_RETRIES = 4


def _check_id(value: str) -> None:
    if not _VALID_ID.fullmatch(value):
        raise ValueError(f"无效 ID：{value!r}，请使用字母、数字、下划线或连字符")


def _json(value: Worker | Workflow | Run) -> str:
    return json.dumps(to_dict(value), ensure_ascii=False, indent=2) + "\n"


def write_text_atomic(path: Path, content: str) -> None:
    """同目录临时文件 + fsync + Windows 短退避重试原子替换，防止多开或读锁导致半截坏文件。"""
    temporary: Path | None = None
    try:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = content.encode("utf-8")
            if path.exists() and path.read_bytes() == encoded:
                return
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(1, _REPLACE_RETRIES + 1):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == _REPLACE_RETRIES:
                        raise
                    time.sleep(0.03 * attempt)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except OSError as error:
        raise RuntimeError(f"无法原子保存文件 {path}：{error}") from error


@contextmanager
def _acquire_file_lock(lock_path: Path, timeout: float = 10.0) -> Iterator[None]:
    """基于操作系统内核文件锁实现跨进程互斥，进程崩溃时由内核自动释放句柄。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with lock_path.open("a+b") as handle:
        fd = handle.fileno()
        if os.fstat(fd).st_size == 0:
            handle.write(b"0")
            handle.flush()
        locked = False
        while True:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"等待数据目录写入锁超时（{lock_path}），请检查是否有其他实例占用"
                    ) from exc
                time.sleep(0.02)
        try:
            yield
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass


class Store:
    """纯文件本地存储：workers/、workflows/ 与 runs/ 均为可读 JSON，凭据存于 SecretBox。"""

    def __init__(
        self,
        data_dir: Path,
        *,
        backup_dir: Path | None = None,
        secrets: SecretBox | None = None,
    ) -> None:
        self.data_dir = data_dir.resolve()
        self.backup_dir = (backup_dir or (self.data_dir.parent / "config" / "backups")).resolve()
        self._secrets = secrets or SecretBox()
        self.corrupted_files: list[str] = []
        self._lock_path = self.data_dir / ".ctflow.lock"
        self._lock_depth = 0
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def write_lock(self, timeout: float = 10.0) -> Iterator[None]:
        """可重入跨进程排他锁：保护 Worker/Workflow 增删改、快照创建与回滚临界区。"""
        if self._lock_depth > 0:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        with _acquire_file_lock(self._lock_path, timeout=timeout):
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1

    def close(self) -> None:
        """纯文件存储无常驻句柄，保留对称关闭入口供调用方统一清理。"""

    def _dir(self, kind: str) -> Path:
        folder = (self.data_dir / kind).resolve()
        if not folder.is_relative_to(self.data_dir):
            raise ValueError(f"存储目录越界：{folder}")
        return folder

    def _file_path(self, kind: str, prefix: str, identifier: str) -> Path:
        _check_id(identifier)
        # 统一添加类型前缀（如 worker-con.json），避开 Windows CON/AUX/NUL 等保留设备名限制。
        path = (self._dir(kind) / f"{prefix}-{identifier}.json").resolve()
        if not path.is_relative_to(self.data_dir):
            raise ValueError(f"存储路径越出数据目录：{path}")
        return path

    def _check_case_collision(self, kind: str, prefix: str, identifier: str) -> None:
        folder = self._dir(kind)
        if not folder.is_dir():
            return
        head = f"{prefix}-"
        for item in folder.glob(f"{prefix}-*.json"):
            existing_id = item.stem[len(head) :]
            if existing_id.casefold() == identifier.casefold() and existing_id != identifier:
                raise ValueError(f"ID 仅大小写不同会导致文件冲突：{identifier!r} / {existing_id!r}")

    def _iter_records(self, kind: str, prefix: str) -> list[tuple[str, dict]]:
        folder = self._dir(kind)
        if not folder.is_dir():
            return []
        head = f"{prefix}-"
        records: list[tuple[str, dict]] = []
        for path in sorted(folder.glob(f"{prefix}-*.json"), key=lambda p: p.name.casefold()):
            rel = path.relative_to(self.data_dir).as_posix()
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("顶层不是 JSON 对象")
                file_id = path.stem[len(head) :]
                _check_id(file_id)
                records.append((file_id, raw))
            except (OSError, ValueError, KeyError, TypeError):
                if rel not in self.corrupted_files:
                    self.corrupted_files.append(rel)
        return records

    # ==================== Worker 存取 ====================
    def _hydrate_worker(self, data: dict) -> Worker:
        wid = str(data["id"])
        _check_id(wid)
        return Worker(
            id=wid,
            name=str(data["name"]),
            model=str(data["model"]),
            prompt=str(data.get("prompt", "")),
            skills=list(data.get("skills", [])),
            base_url=str(data.get("base_url", "")),
            api_key=self._secrets.get(f"worker:{wid}"),
            extra_body=dict(data.get("extra_body", {})),
            mcp_servers=list(data.get("mcp_servers", [])),
        )

    def save_worker(self, worker: Worker) -> None:
        _check_id(worker.id)
        if worker.id.casefold() == ARCHITECT_ID:
            raise ValueError(f"ID {ARCHITECT_ID!r} 为内置编排助手保留标识，请使用其他 ID")
        with self.write_lock():
            self._check_case_collision("workers", "worker", worker.id)
            path = self._file_path("workers", "worker", worker.id)
            write_text_atomic(path, _json(worker))
            self._secrets.set(f"worker:{worker.id}", worker.api_key)

    def list_workers(self) -> list[Worker]:
        workers: list[Worker] = []
        for _, raw in self._iter_records("workers", "worker"):
            try:
                if str(raw.get("id", "")).casefold() == ARCHITECT_ID:
                    continue
                workers.append(self._hydrate_worker(raw))
            except (ValueError, KeyError, TypeError):
                continue
        workers.sort(key=lambda w: (w.name, w.id))
        return workers

    def get_worker(self, id_or_name: str) -> Worker:
        for _, raw in self._iter_records("workers", "worker"):
            if str(raw.get("id", "")).casefold() == id_or_name.casefold():
                return self._hydrate_worker(raw)
        matches = [w for w in self.list_workers() if w.name == id_or_name]
        if not matches:
            raise ValueError(f"找不到 workers：{id_or_name}")
        if len(matches) > 1:
            raise ValueError(f"workers 名称有歧义，请使用 ID：{id_or_name}")
        return matches[0]

    def delete_worker(self, identifier: str) -> None:
        _check_id(identifier)
        if identifier.casefold() == ARCHITECT_ID:
            raise ValueError("内置编排助手不可删除")
        with self.write_lock():
            for workflow in self.list_workflows():
                if any(
                    node.worker_id.casefold() == identifier.casefold() for node in workflow.nodes
                ):
                    raise ValueError(f"Worker {identifier} 正被工作流 {workflow.id} 引用，无法删除")
            path = self._file_path("workers", "worker", identifier)
            folder = self._dir("workers")
            exact = folder.is_dir() and any(p.name == path.name for p in folder.iterdir())
            if not exact:
                raise ValueError(f"找不到 workers：{identifier}")
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                raise RuntimeError(f"无法删除 workers/{identifier}：{error}") from error
            self._secrets.delete(f"worker:{identifier}")

    # ==================== Workflow 存取与乐观并发 ====================
    def workflow_mtime(self, identifier: str) -> int | None:
        """返回指定工作流磁盘文件的纳秒修改时间戳，供保存前检测外部并发更新。"""
        try:
            path = self._file_path("workflows", "workflow", identifier)
        except ValueError:
            return None
        try:
            return path.stat().st_mtime_ns if path.is_file() else None
        except OSError:
            return None

    def save_workflow(self, workflow: Workflow, *, expected_mtime: int | None = None) -> int:
        _check_id(workflow.id)
        topological_layers(workflow)
        for node in workflow.nodes:
            _check_id(node.id)
            _check_id(node.worker_id)
        with self.write_lock():
            self._check_case_collision("workflows", "workflow", workflow.id)
            path = self._file_path("workflows", "workflow", workflow.id)
            if expected_mtime is not None and path.is_file():
                current_mtime = path.stat().st_mtime_ns
                if current_mtime != expected_mtime:
                    raise ValueError(
                        f"工作流 {workflow.id} 已被外部实例或编排助手更新，"
                        "已拦截旧副本覆盖（请重新加载最新配置）"
                    )
            write_text_atomic(path, _json(workflow))
            return path.stat().st_mtime_ns

    def list_workflows(self) -> list[Workflow]:
        workflows: list[Workflow] = []
        for _, raw in self._iter_records("workflows", "workflow"):
            try:
                workflows.append(Workflow.from_dict(raw))
            except (ValueError, KeyError, TypeError):
                rel = f"workflows/workflow-{raw.get('id', '?')}.json"
                if rel not in self.corrupted_files:
                    self.corrupted_files.append(rel)
        workflows.sort(key=lambda wf: (wf.name, wf.id))
        return workflows

    def get_workflow(self, id_or_name: str) -> Workflow:
        for _, raw in self._iter_records("workflows", "workflow"):
            if str(raw.get("id", "")).casefold() == id_or_name.casefold():
                return Workflow.from_dict(raw)
        matches = [wf for wf in self.list_workflows() if wf.name == id_or_name]
        if not matches:
            raise ValueError(f"找不到 workflows：{id_or_name}")
        if len(matches) > 1:
            raise ValueError(f"workflows 名称有歧义，请使用 ID：{id_or_name}")
        return matches[0]

    def delete_workflow(self, identifier: str) -> None:
        _check_id(identifier)
        with self.write_lock():
            path = self._file_path("workflows", "workflow", identifier)
            folder = self._dir("workflows")
            exact = folder.is_dir() and any(p.name == path.name for p in folder.iterdir())
            if not exact:
                raise ValueError(f"找不到 workflows：{identifier}")
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                raise RuntimeError(f"无法删除 workflows/{identifier}：{error}") from error

    def validate_managed_file(self, path: Path) -> None:
        """校验外接 MCP 落盘的受管 JSON 文件是否符合数据契约（不合法则由调用方回退）。"""
        resolved = path.resolve()
        workers_dir = self._dir("workers")
        workflows_dir = self._dir("workflows")

        if resolved.parent == workers_dir:
            if resolved.suffix != ".json" or not resolved.stem.startswith("worker-"):
                raise ValueError("workers/ 下文件名必须为 worker-<id>.json")
            file_id = resolved.stem[len("worker-") :]
            _check_id(file_id)
            if file_id.casefold() == ARCHITECT_ID:
                raise ValueError(f"保留标识 {ARCHITECT_ID!r} 不可写入 workers/ 目录")
            self._check_case_collision("workers", "worker", file_id)
            try:
                raw = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Worker 文件不是合法的 UTF-8 JSON：{exc}") from exc
            if not isinstance(raw, dict):
                raise ValueError("Worker JSON 顶层必须是对象")
            if "content" in raw and "id" not in raw:
                raise ValueError("请勿在文件内容外层包裹 content 对象，直接写入 Worker 顶层字段")
            if "api_key" in raw:
                raise ValueError("严禁在 Worker JSON 文件中包含 api_key 字段")
            if str(raw.get("id", "")) != file_id:
                raise ValueError(f"文件名 ID ({file_id}) 与 JSON 内 id ({raw.get('id')!r}) 不一致")
            try:
                worker = self._hydrate_worker(raw)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Worker 字段缺失或类型无效：{exc}") from exc
            if not worker.name.strip() or not worker.model.strip():
                raise ValueError("Worker 的 name 与 model 不能为空")
            return

        if resolved.parent == workflows_dir:
            if resolved.suffix != ".json" or not resolved.stem.startswith("workflow-"):
                raise ValueError("workflows/ 下文件名必须为 workflow-<id>.json")
            file_id = resolved.stem[len("workflow-") :]
            _check_id(file_id)
            self._check_case_collision("workflows", "workflow", file_id)
            try:
                raw = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Workflow 文件不是合法的 UTF-8 JSON：{exc}") from exc
            if not isinstance(raw, dict):
                raise ValueError("Workflow JSON 顶层必须是对象")
            if "content" in raw and "id" not in raw:
                raise ValueError("请勿在文件内容外层包裹 content 对象，直接写入 Workflow 顶层字段")
            if str(raw.get("id", "")) != file_id:
                raise ValueError(f"文件名 ID ({file_id}) 与 JSON 内 id ({raw.get('id')!r}) 不一致")
            try:
                wf = Workflow.from_dict(raw)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Workflow 字段缺失或节点格式无效：{exc}") from exc
            for node in wf.nodes:
                _check_id(node.id)
                _check_id(node.worker_id)
            catalog = {w.id: w for w in self.list_workers()}
            validate_workflow(wf, catalog)
            return

        raise ValueError(
            "仅允许在受管根目录直接读写 workers/worker-<id>.json 或 workflows/workflow-<id>.json"
        )

    # ==================== Run 存取 ====================
    def save_run(self, run: Run) -> None:
        _check_id(run.id)
        if len({key.casefold() for key in run.nodes}) != len(run.nodes):
            raise ValueError("节点 ID 仅大小写不同会导致结果文件冲突")
        for node_id, node in run.nodes.items():
            _check_id(node_id)
            if node.node_id != node_id:
                raise ValueError(f"节点结果 ID 与字典键不一致：{node.node_id!r} / {node_id!r}")
        self._check_case_collision("runs", "run", run.id)
        path = self._file_path("runs", "run", run.id)
        write_text_atomic(path, _json(run))

    def get_run(self, identifier: str) -> Run:
        _check_id(identifier)
        for _, raw in self._iter_records("runs", "run"):
            if str(raw.get("id", "")).casefold() == identifier.casefold():
                return Run.from_dict(raw)
        raise ValueError(f"找不到运行记录：{identifier}")

    def list_runs(self, limit: int = 50) -> list[Run]:
        if limit < 0:
            raise ValueError("运行记录数量不能为负数")
        if limit == 0:
            return []
        runs: list[Run] = []
        for _, raw in self._iter_records("runs", "run"):
            try:
                runs.append(Run.from_dict(raw))
            except (ValueError, KeyError, TypeError):
                continue
        runs.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return runs[:limit]

    # ==================== 快照备份与一键回滚 ====================
    def create_backup(self) -> Path:
        """在编排助手修改配置前自动将 workers/ 与 workflows/ 快照备份至隔离区。"""
        with self.write_lock():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            snap_dir = self.backup_dir / f"snap-{stamp}-{new_id()[:6]}"
            snap_dir.mkdir(parents=True, exist_ok=True)
            for kind in ("workers", "workflows"):
                src = self._dir(kind)
                dst = snap_dir / kind
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    dst.mkdir(parents=True, exist_ok=True)
            snapshots = sorted(
                (p for p in self.backup_dir.glob("snap-*") if p.is_dir()),
                key=lambda p: p.name,
            )
            for stale in snapshots[:-_MAX_BACKUPS]:
                shutil.rmtree(stale, ignore_errors=True)
            return snap_dir

    def rollback_latest(self) -> str:
        """在写入锁内按文件原子同步恢复快照，避免删除受管目录导致外接进程句柄报错。"""
        with self.write_lock():
            if not self.backup_dir.is_dir():
                raise ValueError("尚无可用的配置备份快照")
            snapshots = sorted(
                (p for p in self.backup_dir.glob("snap-*") if p.is_dir()),
                key=lambda p: p.name,
            )
            if not snapshots:
                raise ValueError("尚无可用的配置备份快照")
            latest = snapshots[-1]
            for kind in ("workers", "workflows"):
                target = self._dir(kind)
                target.mkdir(parents=True, exist_ok=True)
                src = latest / kind
                keep_names: set[str] = set()
                if src.is_dir():
                    for item in src.iterdir():
                        if item.is_file():
                            keep_names.add(item.name)
                            write_text_atomic(target / item.name, item.read_text(encoding="utf-8"))
                for existing in list(target.iterdir()):
                    if existing.name not in keep_names:
                        if existing.is_dir():
                            shutil.rmtree(existing, ignore_errors=True)
                        else:
                            existing.unlink(missing_ok=True)
            self.corrupted_files.clear()
            w_cnt = len(self.list_workers())
            wf_cnt = len(self.list_workflows())
            return f"{latest.name}（已恢复 {w_cnt} 个 Worker、{wf_cnt} 套工作流）"
