# CTflow 终端工作流原型

这是本地模拟、可实际试玩的真实 Textual 终端应用。五版布局共享一份模型及运行状态，围绕命令输入、节点选择、预设 Worker 装配和 Markdown 成稿组织交互。自然语言输入会生成本地示范方案，内容来自本地模拟逻辑；此功能未接入真实 AI。

## 启动与五版布局

在 PowerShell 中运行：

```powershell
E:\CTflow\.venv\Scripts\python.exe E:\CTflow\prototypes\tui-terminal\app.py
```

使用 `--design` 指定启动布局，默认值为 `harbor`：

```powershell
E:\CTflow\.venv\Scripts\python.exe E:\CTflow\prototypes\tui-terminal\app.py --design flux
```

布局可选 `harbor`、`ribbon`、`flux`、`folio`、`ledger`。F1 至 F5 按此顺序提供试玩切换；日常操作使用命令，界面顶部没有五个布局按钮。切换布局保留当前节点、装配及运行状态。

| 方案 | 侧重点 |
| --- | --- |
| `harbor` | 蓝岸：画布、正文和右侧装配卡片的均衡布局。 |
| `ribbon` | 路线：压缩拓扑和边框，给正文留下更多终端行。 |
| `flux` | 流向：节点内展示状态，运行节点的入边沿箭头方向流动。 |
| `folio` | 书页：收拢拓扑，突出单栏 Markdown 成稿。 |
| `ledger` | 叙事：阅读在上、流程在下，采用轻量分隔。 |

可通过 `E:\CTflow\prototypes\tui-terminal\start.ps1` 直接启动 `flux`。独立使用源码包时，先执行 `python -m pip install -r requirements.txt`，再执行 `python app.py --design flux`。

## 五个主命令

| 命令 | 用法 |
| --- | --- |
| `/flow` | 默认打开纯文本拓扑编辑器；`/flow review`、`/flow serial`、`/flow new` 加载对应预设。 |
| `/node` | 选择节点，例如 `/node A`；`/node A @research` 装配预设 Worker；`/node A after B,C` 设置直接依赖；`/node add @writer 名称` 添加节点。 |
| `/run` | 启动当前方案；`/run stop` 停止；`/run defeat` 启动模拟失败场景。 |
| `/view` | `/view flow`、`/view run`、`/view result` 切换三种内容；`/view harbor` 等布局名切换五版布局。 |
| `/export [文件名]` | 将 Markdown 导出至 `E:\CTflow\artifacts\tui-terminal\exports`；文件名可以省略。 |

默认评审流程为 A、B 并行，C、D 等待 A 和 B 全部完成，E 等待 C 和 D 全部完成。Worker 是可复用预设，Node 是引用预设的任务实例；可选 Worker 为 `research`、`code`、`pro`、`con`、`judge`、`writer`。

## 选择、装配与显示

右侧节点卡片负责选择和装配。编排视图中点击卡片或键盘选择后按 Enter，打开 WorkerPicker 并装配预设。运行期间卡片支持只读选择；装配、任务与依赖编辑在运行结束后进行。装配变更会重置此前运行的结果。

画布只读，拓扑编辑通过 `/flow` 的纯文本编辑器或 `/node` 命令完成。界面展示简单 Markdown，状态采用下列四个值：

| 状态 | 含义 |
| --- | --- |
| `waiting` | 等待启动或等待上游依赖。 |
| `running` | 正在模拟执行。 |
| `completed` | 正常结束。 |
| `defeat` | 失败、中止或上游受阻。 |

界面无百分比。五版仅在 `running` 节点行显示点动画；`flux` 额外在指向运行节点的入边上显示字符流箭头。失败节点阻止依赖它的下游启动，已运行的平行分支可完成。

输入 `/` 打开命令候选，按输入内容模糊筛选；↑、↓ 选择候选，Tab 补全，Enter 提交，Esc 关闭当前候选或弹层。Ctrl+Q 退出。

## 六步试玩

1. 使用默认启动命令打开 `harbor`，输入一个自然语言目标，观察本地示范方案和右侧节点卡。
2. 输入 `/flow` 查看纯文本拓扑编辑器，Ctrl+S 应用、Esc 返回；使用 `/flow review` 加载五节点评审预设。点击 A 卡片打开 WorkerPicker，选择 `research`。
3. 输入 `/node add @writer 汇总成稿` 添加节点，记下界面分配的节点 ID，再用 `/node 节点ID after E` 设置依赖。输入 `/node A @code` 练习命令装配。
4. 输入 `/run` 自动进入执行视图，观察 A、B 并行与后续依赖屏障。运行时点击卡片查看节点；按 F1 至 F5 切换布局，观察共享状态及 `flux` 入边字符流。
5. 等全部完成后输入 `/view result` 查看 Markdown，再输入 `/export 试玩报告.md` 导出。重新加载 `/flow review` 并执行 `/run defeat`，观察失败分支及受阻下游；再次加载预设，执行 `/run` 后输入 `/run stop` 查看中止状态。
6. 输入 `/flow serial` 试玩串行预设，再输入 `/flow new` 从新方案开始。输入 `/` 练习模糊筛选、↑↓、Tab、Enter 和 Esc，最后按 Ctrl+Q 退出。

## 验证记录

模型测试覆盖并行依赖与 AND 屏障、事件时间步长一致性、运行中编辑拒绝、Worker 装配与重跑、节点和依赖编辑原子性、失败及停止、运行结果重置、Token 汇总、时间单调性和 Markdown 产物。运行命令：

```powershell
E:\CTflow\.venv\Scripts\python.exe -m pytest E:\CTflow\prototypes\tui-terminal\test_model.py -q
```

49 项测试已通过，包含 27 项模型测试与 22 项真实 Textual / 字符渲染测试；Ruff 检查和格式检查通过。五版均验证 136×40、100×30、80×24 字符尺寸，并检查鼠标装配、键盘选择、命令补全、Esc 返回、拓扑编辑、只读画布、依赖方向、动画冻结、失败报告与 UTF-8 导出。

预览和验收记录位于 `E:\CTflow\artifacts\tui-terminal\refined`：`five-designs.png` 为五版总览，`flux-running.gif` 为真实 Textual 字符帧动图，`assembly.png` 展示预设卡片装配，`result.png` 展示 Markdown 成稿，`verification.json` 与 `test-results.xml` 保存检查结果。PNG 按原始 SVG 的字符格坐标生成，以保留中文双宽字符及时间线的准确位置。
