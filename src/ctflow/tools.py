"""发现并读取外置技能目录 extensions/skills/<name>/SKILL.md 中的技能正文。"""

from __future__ import annotations

import re
from pathlib import Path

_SKILL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not _SKILL_ID.fullmatch(name):
        raise ValueError(f"技能 ID 无效：{name!r}，请使用不带 @ 的字母、数字、下划线或连字符")


def _skill_path(skills_dir: Path, name: str) -> Path:
    _check_name(name)
    root = skills_dir.resolve()
    path = root / name / "SKILL.md"
    # 目录与文件必须留在技能根目录内，拒绝符号链接越界跳转。
    if skills_dir.is_symlink() or path.resolve() != path:
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


def discover_skills(skills_dir: Path) -> dict[str, Path]:
    """返回技能 ID 到绝对路径的映射；损坏的技能立即报错。"""
    if skills_dir.is_symlink() or skills_dir.resolve() != skills_dir.absolute():
        raise ValueError("skills 目录不能是符号链接或目录联接")
    if not skills_dir.exists():
        return {}
    if not skills_dir.is_dir():
        raise ValueError("skills 必须是目录")
    skills: dict[str, Path] = {}
    for folder in sorted(skills_dir.iterdir(), key=lambda item: item.name):
        if folder.is_dir() or folder.is_symlink():
            path = _skill_path(skills_dir, folder.name)
            _read_skill(path, folder.name)
            skills[folder.name] = path
    return skills


def read_skills(skills_dir: Path, names: list[str]) -> dict[str, str]:
    """按 Worker.skills 顺序读取各技能正文，返回 {skill_id: text}。"""
    result: dict[str, str] = {}
    for name in names:
        path = _skill_path(skills_dir, name)
        result[name] = _read_skill(path, name)
    return result
