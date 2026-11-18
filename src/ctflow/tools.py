"""发现并读取 workspace/skill/<name>/SKILL.md 中的技能正文。"""

import re
from pathlib import Path

_SKILL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not _SKILL_ID.fullmatch(name):
        raise ValueError(f"技能 ID 无效：{name!r}，请使用不带 @ 的字母、数字、下划线或连字符")


def _skill_path(workspace: Path, name: str) -> Path:
    _check_name(name)
    workspace = workspace.resolve()
    root = workspace / "skill"
    path = root / name / "SKILL.md"
    # 目录和文件都必须留在技能根目录内，避免符号链接把技能读取导向其他文件。
    if root.resolve() != root or path.resolve() != path:
        raise ValueError(f"技能 @{name} 的路径不能包含符号链接或目录联接")
    if not path.is_file():
        raise ValueError(f"技能 @{name} 缺少文件：{path}")
    return path


def _read_skill(path: Path, name: str) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"技能 @{name} 必须是 UTF-8 文本") from exc
    except OSError as exc:
        raise ValueError(f"无法读取技能 @{name}：{exc}") from exc
    if not text.strip():
        raise ValueError(f"技能 @{name} 的 SKILL.md 不能为空")
    return text


def discover_skills(workspace: Path) -> dict[str, Path]:
    """返回技能 ID 到绝对路径的映射；损坏的技能立即报错。"""
    workspace = workspace.resolve()
    root = workspace / "skill"
    if root.is_symlink() or root.resolve() != root:
        raise ValueError("skill 目录不能是符号链接或目录联接")
    if not root.exists():
        return {}
    if not root.is_dir():
        raise ValueError("skill 必须是目录")
    skills = {}
    for folder in sorted(root.iterdir(), key=lambda item: item.name):
        if folder.is_dir() or folder.is_symlink():
            path = _skill_path(workspace, folder.name)
            _read_skill(path, folder.name)
            skills[folder.name] = path
    return skills


def read_skills(workspace: Path, names: list[str]) -> str:
    """按 Worker.skills 顺序拼接正文；名称使用不带 @ 的技能 ID。"""
    sections = []
    for name in names:
        path = _skill_path(workspace, name)
        skill_text = _read_skill(path, name)
        sections.append(f"技能 @{name}：\n{skill_text}")
    return "\n\n".join(sections)
