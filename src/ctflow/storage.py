"""SQLite 保存一致快照，JSON、Mermaid 和 Markdown 方便直接阅读。"""

import json
import os
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from tempfile import NamedTemporaryFile

from ctflow.graph import to_mermaid
from ctflow.models import Run, Worker, Workflow, to_dict
from ctflow.secrets import SecretBox


def _check_id(value: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError(f"无效 ID：{value!r}，请使用字母、数字、下划线或连字符")


def _json(value: Worker | Workflow | Run) -> str:
    return json.dumps(to_dict(value), ensure_ascii=False, indent=2) + "\n"


def _write_atomic(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 运行中频繁保存快照时，跳过没有变化的节点结果。
            if path.exists() and path.read_bytes() == content.encode("utf-8"):
                return
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except OSError as error:
        raise RuntimeError(f"无法原子保存文件 {path}：{error}") from error


class Store:
    """本地轻量存储；调用者负责在退出时调用 close。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._secrets = SecretBox(self.data_dir)
        self._db = sqlite3.connect(self._path("ctflow.db"))
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS workers (
                    id TEXT PRIMARY KEY COLLATE NOCASE,
                    name TEXT NOT NULL,
                    snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY COLLATE NOCASE,
                    name TEXT NOT NULL,
                    snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY COLLATE NOCASE,
                    created_at TEXT NOT NULL,
                    snapshot TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_created_at ON runs(created_at DESC);
                """
            )
        self._migrate_worker_keys()

    def _migrate_worker_keys(self) -> None:
        """把旧版明文密钥就地升级为当前平台的加密密文。"""
        rows = self._db.execute("SELECT id, snapshot FROM workers").fetchall()
        with self._db:
            for identifier, snapshot in rows:
                data = json.loads(snapshot)
                api_key = data.get("api_key", "")
                if not api_key or self._secrets.is_encrypted(api_key):
                    continue
                data["api_key"] = self._secrets.protect(api_key)
                self._db.execute(
                    "UPDATE workers SET snapshot = ? WHERE id = ?",
                    (json.dumps(data, ensure_ascii=False), identifier),
                )

    def close(self) -> None:
        self._db.close()

    def _path(self, *parts: str) -> Path:
        path = self.data_dir.joinpath(*parts)
        if not path.resolve().is_relative_to(self.data_dir):
            raise ValueError(f"存储路径越出数据目录：{path}")
        return path

    def _save(
        self, table: str, identifier: str, label: str, snapshot: str, files: dict[Path, str]
    ) -> None:
        # 先完成可读文件导出，失败时保留数据库中上一份完整快照。
        try:
            with self._db:
                row = self._db.execute(
                    f"SELECT id FROM {table} WHERE id = ?", (identifier,)
                ).fetchone()
                if row is not None and row[0] != identifier:
                    raise ValueError(f"ID 仅大小写不同会导致文件冲突：{identifier!r} / {row[0]!r}")
                for path, content in files.items():
                    _write_atomic(path, content)
                column = "created_at" if table == "runs" else "name"
                self._db.execute(
                    f"INSERT INTO {table} (id, {column}, snapshot) VALUES (?, ?, ?) "
                    f"ON CONFLICT(id) DO UPDATE SET {column}=excluded.{column}, "
                    "snapshot=excluded.snapshot",
                    (identifier, label, snapshot),
                )
        except sqlite3.Error as error:
            raise RuntimeError(f"无法保存 {table}/{identifier} 到 SQLite：{error}") from error

    def _get_named(self, table: str, id_or_name: str) -> dict:
        row = self._db.execute(
            f"SELECT snapshot FROM {table} WHERE id = ?", (id_or_name,)
        ).fetchone()
        if row is not None:
            return json.loads(row[0])
        rows = self._db.execute(
            f"SELECT snapshot FROM {table} WHERE name = ?", (id_or_name,)
        ).fetchall()
        if not rows:
            raise ValueError(f"找不到 {table}：{id_or_name}")
        if len(rows) > 1:
            raise ValueError(f"{table} 名称有歧义，请使用 ID：{id_or_name}")
        return json.loads(rows[0][0])

    def save_worker(self, worker: Worker) -> None:
        _check_id(worker.id)
        data = asdict(worker)
        data["api_key"] = self._secrets.protect(worker.api_key)
        snapshot = json.dumps(data, ensure_ascii=False)
        self._save(
            "workers",
            worker.id,
            worker.name,
            snapshot,
            {self._path("workers", f"worker-{worker.id}.json"): _json(worker)},
        )

    def list_workers(self) -> list[Worker]:
        rows = self._db.execute("SELECT snapshot FROM workers ORDER BY name, id").fetchall()
        return [self._worker(json.loads(row[0])) for row in rows]

    def get_worker(self, id_or_name: str) -> Worker:
        return self._worker(self._get_named("workers", id_or_name))

    def _worker(self, data: dict) -> Worker:
        values = dict(data)
        values["api_key"] = self._secrets.unprotect(values.get("api_key") or "")
        return Worker(**values)

    def delete_worker(self, id: str) -> None:
        _check_id(id)
        for workflow in self.list_workflows():
            if any(node.worker_id.casefold() == id.casefold() for node in workflow.nodes):
                raise ValueError(f"Worker {id} 正被工作流 {workflow.id} 引用，无法删除")
        self._delete("workers", id, [self._path("workers", f"worker-{id}.json")])

    def save_workflow(self, workflow: Workflow) -> None:
        _check_id(workflow.id)
        for node in workflow.nodes:
            _check_id(node.id)
            _check_id(node.worker_id)
        snapshot = _json(workflow)
        self._save(
            "workflows",
            workflow.id,
            workflow.name,
            snapshot,
            {
                self._path("workflows", f"workflow-{workflow.id}.json"): snapshot,
                self._path("workflows", f"workflow-{workflow.id}.mmd"): to_mermaid(workflow),
            },
        )

    def list_workflows(self) -> list[Workflow]:
        rows = self._db.execute("SELECT snapshot FROM workflows ORDER BY name, id").fetchall()
        return [Workflow.from_dict(json.loads(row[0])) for row in rows]

    def get_workflow(self, id_or_name: str) -> Workflow:
        return Workflow.from_dict(self._get_named("workflows", id_or_name))

    def delete_workflow(self, id: str) -> None:
        _check_id(id)
        self._delete(
            "workflows",
            id,
            [
                self._path("workflows", f"workflow-{id}.json"),
                self._path("workflows", f"workflow-{id}.mmd"),
            ],
        )

    def _delete(self, table: str, identifier: str, paths: list[Path]) -> None:
        row = self._db.execute(
            f"SELECT id FROM {table} WHERE id = ? COLLATE BINARY", (identifier,)
        ).fetchone()
        if row is None:
            raise ValueError(f"找不到 {table}：{identifier}")
        try:
            with self._db:
                for path in paths:
                    path.unlink(missing_ok=True)
                self._db.execute(f"DELETE FROM {table} WHERE id = ?", (identifier,))
        except (OSError, sqlite3.Error) as error:
            raise RuntimeError(f"无法删除 {table}/{identifier}：{error}") from error

    def save_run(self, run: Run) -> None:
        _check_id(run.id)
        if len({key.casefold() for key in run.nodes}) != len(run.nodes):
            raise ValueError("节点 ID 仅大小写不同会导致结果文件冲突")
        snapshot = _json(run)
        public_nodes = to_dict(run)["nodes"]
        files = {self._path("runs", f"run-{run.id}", "run.json"): snapshot}
        for node_id, node in run.nodes.items():
            _check_id(node_id)
            if node.node_id != node_id:
                raise ValueError(f"节点结果 ID 与字典键不一致：{node.node_id!r} / {node_id!r}")
            public_node = public_nodes[node_id]
            result = public_node["output"]
            if public_node["error"]:
                result += f"\n\n## 错误\n\n{public_node['error']}"
            files[self._path("runs", f"run-{run.id}", f"node-{node_id}.md")] = result + "\n"
        self._save("runs", run.id, run.created_at, snapshot, files)

    def get_run(self, id: str) -> Run:
        _check_id(id)
        row = self._db.execute("SELECT snapshot FROM runs WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise ValueError(f"找不到运行记录：{id}")
        return Run.from_dict(json.loads(row[0]))

    def list_runs(self, limit: int = 50) -> list[Run]:
        if limit < 0:
            raise ValueError("运行记录数量不能为负数")
        rows = self._db.execute(
            "SELECT snapshot FROM runs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Run.from_dict(json.loads(row[0])) for row in rows]
