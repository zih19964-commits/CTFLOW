# 外置技能目录

将可复用技能包放入 `extensions/skills/<name>/SKILL.md`，文件采用 UTF-8 编码。

在工作台打开 `/worker <id>` 配置界面，即可在 `@技能` 多选列表中勾选对应技能。节点或单聊运行时，所选 `SKILL.md` 正文会装入 `<mounted_skills>` 上下文标签中发送给模型。
