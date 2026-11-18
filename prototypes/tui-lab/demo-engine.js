/* 五套界面共用的本地演示引擎。模拟调度与结果，完全由当前页面驱动。 */
(function () {
  'use strict';

  const workers = [
    { id: 'research', name: '资料研究', model: 'qwen-plus', prompt: '围绕目标查找资料，整理事实、来源与尚待核实的问题。', tools: ['search_web'], skill: '证据整理' },
    { id: 'code', name: '技术分析', model: 'qwen-plus', prompt: '分析实现路径、数据结构、性能与边界，给出具体可行的建议。', tools: ['read_file', 'list_files'], skill: '技术评估' },
    { id: 'pro', name: '价值分析', model: 'qwen-plus', prompt: '读取直接上游的最终结果，明确价值、优势及成立条件。', tools: [], skill: '论证检查' },
    { id: 'con', name: '风险评审', model: 'qwen-plus', prompt: '读取直接上游的最终结果，查找风险、反例与需要补充的验证。', tools: [], skill: '反向审查' },
    { id: 'judge', name: '综合成稿', model: 'qwen-plus', prompt: '综合直接上游的结论与证据，形成可供决策的最终交付稿。', tools: [], skill: '结构化写作' },
  ];
  const templates = {
    review: { name: '本地课程任务管理器 · 方案评审', task: '评审一个用 Python + SQLite 实现的本地课程任务管理器，给出依据、优点、风险与最终建议。', names: ['收集依据', '分析实现', '总结优点', '评估风险', '形成结论'] },
    research: { name: '一个问题 · 多面研究', task: '研究小团队应该怎样选择本地优先的知识管理工具，比较使用成本、协作方式与数据可控性。', names: ['需求研究', '方案调研', '比较收益', '质疑假设', '研究结论'] },
    launch: { name: '新产品 · 发布工作室', task: '为一款面向大学生的任务管理工具策划发布，明确目标受众、卖点、风险与发布内容。', names: ['受众洞察', '产品解读', '提炼卖点', '审阅文案', '发布方案'] },
  };
  const stateLabels = { idle: '准备就绪', running: '执行中', paused: '已暂停', review: '等待检查', failed: '需要处理', completed: '已完成', stopped: '已停止' };
  const nodeLabels = { pending: '待执行', running: '运行中', completed: '已完成', failed: '失败', blocked: '上游未完成' };

  function createNodes(kind, team) {
    const deps = [[], [], ['A', 'B'], ['A', 'B'], ['C', 'D']];
    return templates[kind].names.map((name, index) => ({
      id: String.fromCharCode(65 + index), name, worker: team[index].id, label: team[index].name,
      deps: [...deps[index]], status: 'pending', progress: 0, output: '', tokens: 0,
      duration: [1.8, 2.6, 1.6, 2.3, 2.1][index], spent: 0, started: null, ended: null,
    }));
  }

  function makeState() {
    const team = structuredClone(workers);
    return {
      status: 'idle', mode: 'plan', task: templates.review.task, workflowName: templates.review.name,
      model: 'qwen-plus', nodes: createNodes('review', team), workers: team, selectedNode: 'A',
      messages: [], elapsed: 0, tokens: 0, scenario: 'normal', activeTab: 'output',
      artifactTab: 'preview', logs: [], history: [], draft: '', commandHistory: [],
      template: 'review', checkpointApproved: false, failureTriggered: false,
      runId: '', runNumber: 0, artifact: '', planRevision: 1,
    };
  }

  function message(s, role, text, type = 'text') {
    s.messages.push({ role, text, type, time: s.elapsed });
  }

  function log(s, node, text) { s.logs.push({ time: s.elapsed, node, text }); }

  function busy(s) { return ['running', 'paused', 'review'].includes(s.status); }

  function plan(s, text, kind) {
    if (busy(s)) throw new Error('当前流程仍在执行。先用 /stop 停止，再开始一个新目标。');
    const selected = kind || (/发布|上线|文案|launch/i.test(text) ? 'launch' : /研究|调研|比较|research/i.test(text) ? 'research' : 'review');
    const template = templates[selected];
    const task = text.trim() || template.task;
    s.template = selected;
    s.task = task;
    s.workflowName = kind || task === template.task ? template.name : `${task.slice(0, 24)}${task.length > 24 ? '…' : ''}`;
    s.nodes = createNodes(selected, s.workers);
    const directWorker = /^@(research|code|pro|con|judge)\s+/.exec(task);
    if (directWorker) {
      const worker = s.workers.find(item => item.id === directWorker[1]);
      s.nodes = [{ ...s.nodes[0], name: worker.name, worker: worker.id, label: worker.name }];
      s.workflowName = `${worker.name} · 独立任务`;
    }
    if (/串行|依次执行/.test(task)) s.nodes.forEach((node, i) => { node.deps = i ? [s.nodes[i - 1].id] : []; });
    s.status = 'idle'; s.tokens = 0; s.elapsed = 0; s.logs = []; s.artifact = '';
    s.selectedNode = 'A'; s.activeTab = 'output'; s.checkpointApproved = false; s.failureTriggered = false;
    s.planRevision += 1;
    message(s, 'user', task);
    message(s, 'assistant', directWorker ? `由 @${directWorker[1]} 独立完成这次任务。可以打开上下文检查装配，或开始执行。` : /串行|依次执行/.test(task)
      ? '已按你的要求安排为串行流程。逐步交接结果，每一步都能查看依据与上下文。'
      : '先让资料与技术两个方向并行，再从价值和风险两侧交叉检查，最后综合成稿。你可以调整团队，或开始执行。', 'plan');
    log(s, '·', `方案 v${s.planRevision} 已生成 · ${s.nodes.length} 个节点`);
  }

  function resultFor(s, node) {
    const target = `任务：${s.task}`;
    const outputs = {
      research: `已整理三类演示依据：目标用户、日常使用场景与数据约束。\n\n${target}\n\n• 核心用户需要快速收集任务、安排截止日期和查看完成情况。\n• 本地优先让离线访问与数据导出更直接。\n• 演示依据 ref-A1：需求访谈样例；ref-A2：场景清单。\n\n待验证：真实用户是否愿意维护分类与优先级。`,
      code: `技术路径：Python 业务层 + SQLite 持久化。\n\n${target}\n\n• 用清晰的任务实体表达标题、状态、日期与所属项目。\n• 建立日期与状态索引，让常用查询保持可控。\n• 把界面、调度与存储分离，关键写入在事务中完成。\n\n演示依据 ref-B1：实体草图；ref-B2：查询与边界清单。`,
      pro: `基于直接上游的两份最终结果：\n\n1. 本地启动成本低，适合个人用户和课程展示。\n2. 模型与流程分离后，可复用同一支 Worker 团队。\n3. 每个结论携带依据，便于人工检查和继续迭代。\n\n成立条件：优先完成收集、安排、完成、回看这条核心路径。`,
      con: `需要关注三个风险：\n\n1. 用户输入含糊时，方案可能围绕错误目标推进。应先展示可编辑计划。\n2. 并行节点需要明确交接边界。下游只读取直接上游的最终输出。\n3. 失败不能抹去已有成果。保留成功节点，重试失败节点及其下游。\n\n建议：在最终成稿前提供人工检查点，并让证据可定位。`,
      judge: `建议围绕核心任务路径完成首版，并用一次真实的完整流程检验可用性。\n\n价值与风险已交叉核对：目标清晰、数据可导出、节点上下文独立、失败可定位。\n\n下一步：用 3 位目标用户验证「收集 → 安排 → 完成 → 回看」，记录卡点后再扩展功能。`,
    };
    return outputs[node.worker];
  }

  function markdown(s) {
    return `# ${s.workflowName}\n\n> CTflow 交互原型 · 本地模拟结果\n\n## 目标\n\n${s.task}\n\n## 综合建议\n\n${s.nodes.filter(n => !s.nodes.some(other => other.deps.includes(n.id))).map(n => n.output).join('\n\n')}\n\n## 执行依据\n\n${s.nodes.map(n => `### ${n.id} · ${n.name}\n\n${n.output}`).join('\n\n')}\n\n## 运行\n\n${s.runId} · ${s.nodes.length} 个节点 · ${s.tokens.toLocaleString()} 模拟 Token · ${s.elapsed.toFixed(1)} 秒\n`;
  }

  function snapshot(s) {
    const item = { id: s.runId, name: s.workflowName, status: s.status, tokens: s.tokens, elapsed: s.elapsed,
      nodes: structuredClone(s.nodes), task: s.task, artifact: s.artifact, logs: structuredClone(s.logs), template: s.template };
    const index = s.history.findIndex(run => run.id === s.runId);
    if (index === -1) s.history.unshift(item); else s.history[index] = item;
    s.history = s.history.slice(0, 12);
  }

  // 只有所有直接上游都成功，节点才会进入就绪队列。
  function schedule(s) {
    for (const node of s.nodes) {
      if (node.status !== 'pending' || !node.deps.every(id => s.nodes.find(n => n.id === id).status === 'completed')) continue;
      const terminal = !s.nodes.some(other => other.deps.includes(node.id));
      if (terminal && node.deps.length && s.scenario === 'checkpoint' && !s.checkpointApproved) {
        s.status = 'review'; s.selectedNode = node.id; s.activeTab = 'context';
        log(s, node.id, '已到人工检查点，等待批准后成稿');
        message(s, 'assistant', '上游分析已完成。先检查已有依据与风险，再批准最终成稿。', 'review');
        return;
      }
      node.status = 'running'; node.started = s.elapsed; node.ended = null;
      log(s, node.id, `${node.name} 开始 · @${node.worker}`);
    }
  }

  function run(s) {
    if (s.status === 'paused') return resume(s);
    if (s.status === 'review') throw new Error('当前在人工检查点。查看依据后用 /approve 批准继续。');
    if (s.status === 'running') throw new Error('流程正在执行，可用 /pause 暂停。');
    if (s.status === 'failed') throw new Error('有失败节点。用 /retry 复用成功结果继续。');
    s.nodes.forEach(node => Object.assign(node, { status: 'pending', progress: 0, spent: 0, started: null, ended: null, output: '', tokens: 0 }));
    s.elapsed = 0; s.tokens = 0; s.artifact = ''; s.logs = []; s.checkpointApproved = false; s.failureTriggered = false;
    s.runNumber += 1; s.runId = `demo-${String(s.runNumber).padStart(3, '0')}`; s.status = 'running';
    message(s, 'assistant', `已启动 ${s.nodes.length} 个节点。点击任一节点，可查看交接内容与执行结果。`, 'run');
    log(s, '·', `${s.runId} 开始 · ${s.scenario === 'failure' ? '失败重试场景' : s.scenario === 'checkpoint' ? '人工检查场景' : '正常执行场景'}`);
    schedule(s);
  }

  function markBlocked(s) {
    let changed;
    do {
      changed = false;
      for (const node of s.nodes) {
        if (node.status === 'pending' && node.deps.some(id => ['failed', 'blocked'].includes(s.nodes.find(n => n.id === id).status))) {
          node.status = 'blocked'; changed = true;
        }
      }
    } while (changed);
  }

  function advance(s, seconds) {
    if (s.status !== 'running') return false;
    s.elapsed = Math.round((s.elapsed + seconds) * 1000) / 1000;
    for (const node of s.nodes.filter(n => n.status === 'running')) {
      node.spent += seconds;
      node.progress = Math.min(100, Math.floor(node.spent / node.duration * 100));
      if (node.spent < node.duration) continue;
      node.ended = s.elapsed;
      const failureNode = s.nodes.find(n => n.worker === 'con');
      if (s.scenario === 'failure' && !s.failureTriggered && node.id === failureNode?.id) {
        node.status = 'failed'; node.tokens += 246; s.tokens += 246; s.failureTriggered = true;
        node.output = '演示故障：风险资料请求超时。\n已完成节点的结果仍可检查。选择重试，将重新执行本节点及其下游。';
        s.selectedNode = node.id; s.activeTab = 'output'; markBlocked(s);
        log(s, node.id, '资料请求超时 · 可重试');
        message(s, 'error', `${node.name} 遇到演示故障。已保留成功节点；/retry 将重试本节点及其下游。`);
      } else {
        node.status = 'completed'; node.output = resultFor(s, node);
        const used = 620 + node.id.charCodeAt(0) * 17; node.tokens += used; s.tokens += used;
        log(s, node.id, `${node.name} 完成 · ${used.toLocaleString()} tok`);
      }
    }
    // 失败时先等同批并行任务结束，再进入可重试状态。
    if (s.nodes.some(n => n.status === 'failed')) {
      if (!s.nodes.some(n => n.status === 'running')) { s.status = 'failed'; snapshot(s); }
      return true;
    }
    if (s.nodes.every(n => n.status === 'completed')) {
      s.status = 'completed'; s.artifact = markdown(s);
      log(s, '·', '全部节点完成 · 产物已就绪');
      message(s, 'assistant', '工作流已完成。结果与每个节点的依据已经整理好，可以打开成稿，或导出本次试玩。', 'completed');
      snapshot(s); return true;
    }
    schedule(s); return true;
  }

  function pause(s) {
    if (s.status !== 'running') throw new Error('当前没有运行中的流程。');
    s.status = 'paused'; log(s, '·', '演示调度已暂停'); message(s, 'system', '已暂停。节点进度已保留，/resume 继续。');
  }

  function resume(s) {
    if (s.status === 'review') throw new Error('请先检查依据，再用 /approve 批准继续。');
    if (s.status !== 'paused') throw new Error('当前没有已暂停的流程。');
    s.status = 'running'; log(s, '·', '恢复执行');
  }

  function approve(s) {
    if (s.status !== 'review') throw new Error('当前没有等待批准的检查点。');
    s.checkpointApproved = true; s.status = 'running'; log(s, '·', '人工检查通过，继续成稿');
    message(s, 'system', '已批准上游分析，继续生成最终成稿。'); schedule(s);
  }

  function stop(s) {
    if (!busy(s)) throw new Error('当前没有需要停止的流程。');
    s.status = 'stopped'; s.nodes.filter(n => n.status === 'running').forEach(n => { n.status = 'pending'; n.ended = s.elapsed; });
    log(s, '·', '流程已停止'); message(s, 'system', '已停止。可以检查已完成结果，或重新运行。'); snapshot(s);
  }

  function retry(s) {
    if (s.status !== 'failed') throw new Error('当前没有失败节点。');
    const retryIds = s.nodes.filter(n => ['failed', 'blocked'].includes(n.status)).map(n => n.id);
    s.nodes.filter(n => retryIds.includes(n.id)).forEach(n => Object.assign(n, { status: 'pending', progress: 0, spent: 0, started: null, ended: null, output: '' }));
    s.status = 'running'; log(s, '·', `重试 ${retryIds.join('、')} · 复用成功节点`);
    message(s, 'assistant', `正在重试 ${retryIds.join('、')}。成功节点的结果已复用。`); schedule(s);
  }

  function addNode(s, workerId, dependencyText) {
    if (busy(s)) throw new Error('先停止当前流程，再调整节点。');
    if (s.status !== 'idle') throw new Error('用 /plan 创建草稿后，再加入节点。');
    const worker = s.workers.find(w => w.id === workerId);
    if (!worker) throw new Error('请选择 research、code、pro、con 或 judge。');
    if (s.nodes.length >= 9) throw new Error('此交互原型支持最多 9 个节点。');
    const deps = dependencyText ? dependencyText.toUpperCase().split(',').map(id => id.trim()).filter(Boolean) : [s.nodes.at(-1).id];
    if (deps.some(id => !s.nodes.some(n => n.id === id))) throw new Error('依赖节点不存在，请使用画布中显示的节点 ID。');
    const id = String.fromCharCode(65 + s.nodes.length);
    s.nodes.push({ id, name: worker.name, worker: worker.id, label: worker.name, deps: [...new Set(deps)], status: 'pending', progress: 0, output: '', tokens: 0, duration: 1.7, spent: 0, started: null, ended: null });
    s.selectedNode = id; s.planRevision += 1;
    message(s, 'system', `已加入 ${id} · @${worker.id}，依赖 ${deps.join('、')}。`); log(s, id, '新节点已加入方案');
  }

  window.CTflowDemo = { makeState, templates, stateLabels, nodeLabels, plan, run, advance, pause, resume, approve, stop, retry, addNode, message, log, busy, markdown };
})();
