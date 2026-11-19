"""外置目录 extensions/skills/<name>/SKILL.md 的技能发现与读取测试。"""

from __future__ import annotations

import pytest

from ctflow.tools import discover_skills, read_skills


def skill_file(skills_dir, name, content="技能正文"):
    path = skills_dir / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_missing_skill_root_is_empty(tmp_path):
    skills_dir = tmp_path / "extensions" / "skills"
    assert discover_skills(skills_dir) == {}
    assert read_skills(skills_dir, []) == {}


def test_discovery_and_ordered_read(tmp_path):
    skills_dir = tmp_path / "extensions" / "skills"
    beta = skill_file(skills_dir, "beta", "第二个正文")
    alpha = skill_file(skills_dir, "alpha", "---\nname: alpha\n---\n第一个正文")
    (skills_dir / "README.md").write_text("说明文件", encoding="utf-8")
    assert discover_skills(skills_dir) == {"alpha": alpha, "beta": beta}
    result = read_skills(skills_dir, ["beta", "alpha"])
    assert list(result.keys()) == ["beta", "alpha"]
    assert result["beta"] == "第二个正文"
    assert "第一个正文" in result["alpha"]


@pytest.mark.parametrize(
    "name", ["@reviewer", "", "..", "../outside", "a/b", "a\\b", "C:/x", "a b"]
)
def test_invalid_ids_are_rejected(tmp_path, name):
    with pytest.raises(ValueError, match="技能 ID 无效"):
        read_skills(tmp_path / "skills", [name])


def test_unknown_skill_raises(tmp_path):
    with pytest.raises(ValueError, match="@missing.*缺少文件"):
        read_skills(tmp_path / "skills", ["missing"])


def test_missing_file_in_discovered_skill_raises(tmp_path):
    skills_dir = tmp_path / "skills"
    (skills_dir / "broken").mkdir(parents=True)
    with pytest.raises(ValueError, match="缺少文件"):
        discover_skills(skills_dir)


@pytest.mark.parametrize("content", [b"\xff\xfeinvalid", b"  \n"])
def test_invalid_utf8_or_empty_skill_raises_during_discovery_and_read(tmp_path, content):
    skills_dir = tmp_path / "skills"
    path = skill_file(skills_dir, "broken")
    path.write_bytes(content)
    for operation in (
        lambda: discover_skills(skills_dir),
        lambda: read_skills(skills_dir, ["broken"]),
    ):
        with pytest.raises(ValueError, match="UTF-8|不能为空"):
            operation()


@pytest.mark.parametrize("target_kind", ["root", "folder", "file"])
def test_symlinks_cannot_redirect_skill_reads(tmp_path, target_kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("外部正文", encoding="utf-8")
    root = tmp_path / "skills"
    try:
        if target_kind == "root":
            root.symlink_to(outside, target_is_directory=True)
        elif target_kind == "folder":
            root.mkdir()
            (root / "linked").symlink_to(outside, target_is_directory=True)
        else:
            (root / "linked").mkdir(parents=True)
            (root / "linked" / "SKILL.md").symlink_to(outside / "SKILL.md")
    except OSError as exc:
        pytest.skip(f"当前 Windows 用户无法创建符号链接：{exc}")
    with pytest.raises(ValueError, match="符号链接|目录联接"):
        read_skills(root, ["linked"])
    with pytest.raises(ValueError, match="符号链接|目录联接"):
        discover_skills(root)
