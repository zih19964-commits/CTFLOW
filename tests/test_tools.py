"""技能读取只访问测试工作区内的 skill 目录。"""

import pytest

from ctflow.tools import discover_skills, read_skills


def skill_file(workspace, name, content="技能正文"):
    path = workspace / "skill" / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_missing_skill_root_is_empty(tmp_path):
    assert discover_skills(tmp_path) == {}
    assert read_skills(tmp_path, []) == ""


def test_discovery_and_ordered_read(tmp_path):
    beta = skill_file(tmp_path, "beta", "第二个正文")
    alpha = skill_file(tmp_path, "alpha", "---\nname: alpha\n---\n第一个正文")
    (tmp_path / "skill" / "README.md").write_text("说明文件", encoding="utf-8")
    assert discover_skills(tmp_path) == {"alpha": alpha, "beta": beta}
    text = read_skills(tmp_path, ["beta", "alpha"])
    assert text == "技能 @beta：\n第二个正文\n\n技能 @alpha：\n---\nname: alpha\n---\n第一个正文"


@pytest.mark.parametrize(
    "name", ["@reviewer", "", "..", "../outside", "a/b", "a\\b", "C:/x", "a b"]
)
def test_invalid_ids_are_rejected(tmp_path, name):
    with pytest.raises(ValueError, match="技能 ID 无效"):
        read_skills(tmp_path, [name])


def test_unknown_skill_raises(tmp_path):
    with pytest.raises(ValueError, match="@missing.*缺少文件"):
        read_skills(tmp_path, ["missing"])


def test_missing_file_in_discovered_skill_raises(tmp_path):
    (tmp_path / "skill" / "broken").mkdir(parents=True)
    with pytest.raises(ValueError, match="缺少文件"):
        discover_skills(tmp_path)


def test_invalid_discovered_id_raises(tmp_path):
    skill_file(tmp_path, "bad name")
    with pytest.raises(ValueError, match="技能 ID 无效"):
        discover_skills(tmp_path)


@pytest.mark.parametrize("content", [b"\xff\xfeinvalid", b"  \n"])
def test_invalid_utf8_or_empty_skill_raises_during_discovery_and_read(tmp_path, content):
    path = skill_file(tmp_path, "broken")
    path.write_bytes(content)
    for operation in (lambda: discover_skills(tmp_path), lambda: read_skills(tmp_path, ["broken"])):
        with pytest.raises(ValueError, match="UTF-8|不能为空"):
            operation()


def test_skill_root_must_be_directory(tmp_path):
    (tmp_path / "skill").write_text("不是目录", encoding="utf-8")
    with pytest.raises(ValueError, match="必须是目录"):
        discover_skills(tmp_path)


@pytest.mark.parametrize("target_kind", ["root", "folder", "file"])
def test_symlinks_cannot_redirect_skill_reads(tmp_path, target_kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("外部正文", encoding="utf-8")
    root = tmp_path / "skill"
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
        read_skills(tmp_path, ["linked"])
    with pytest.raises(ValueError, match="符号链接|目录联接"):
        discover_skills(tmp_path)
