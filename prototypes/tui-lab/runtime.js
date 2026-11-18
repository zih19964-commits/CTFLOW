(function () {
  'use strict';
  const D = window.CTflowDemo;
  const C = window.CTflowUI;
  const { e, icon } = C;
  const designs = ['focus', 'rail', 'canvas', 'pulse', 'studio'];
  const storeKey = 'ctflow.interaction-lab.20261021';
  const saved = localStorage.getItem(storeKey);
  const states = saved ? JSON.parse(saved) : Object.fromEntries(designs.map(id => [id, D.makeState()]));
  let active = designs.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'focus';
  let renderedDesign = '';
  let overlay = null;
  let paletteIndex = 0;
  let paletteDismissed = false;
  let historyIndex = -1;
  let toastTimer;
  let lastTick = performance.now();
  let lastSave = 0;
  const stage = document.getElementById('stage');
  const overlayRoot = document.getElementById('overlay-root');
  const current = () => states[active];
  const commandList = [
    { name: '/plan', detail: '把目标编排为可检查的方案', group: '创建', args: true, hint: '描述目标' },
    { name: '/run', detail: '运行当前方案', group: '执行' },
    { name: '/flow', detail: '展开流程与依赖', group: '查看' },
    { name: '/workers', detail: '装配团队、编辑 Worker', group: '装配' },
    { name: '/pause', detail: '保留进度，暂停执行', group: '执行' },
    { name: '/resume', detail: '从暂停处继续', group: '执行' },
    { name: '/approve', detail: '批准检查点，继续成稿', group: '执行' },
    { name: '/retry', detail: '复用成功结果，重试失败节点', group: '执行' },
    { name: '/inspect', detail: '查看节点结果、上下文与活动', group: '查看', args: true, hint: 'A / B / C…' },
    { name: '/artifact', detail: '打开成稿与 Markdown', group: '产物' },
    { name: '/add', detail: '在方案中加入一个 Worker 节点', group: '装配', args: true, hint: 'worker after A,B' },
    { name: '/model', detail: '选择团队默认模型', group: '装配' },
    { name: '/scenario', detail: '体验正常、失败、人工检查', group: '试玩' },
    { name: '/sessions', detail: '打开本版的试玩运行历史', group: '会话' },
    { name: '/export', detail: '导出当前 Markdown 成稿', group: '产物' },
    { name: '/stop', detail: '停止当前流程', group: '执行' },
    { name: '/new', detail: '开始一个空白会话', group: '会话' },
    { name: '/help', detail: '命令与快捷键', group: '帮助' },
  ];

  // 刷新页面后显式恢复为暂停，避免把离线时间算作演示执行时间。
  for (const s of Object.values(states)) {
    if (s.status === 'running') { s.status = 'paused'; D.message(s, 'system', '上次试玩已恢复到暂停状态。/resume 继续。'); }
  }

  function save() { localStorage.setItem(storeKey, JSON.stringify(states)); }

  function toast(text) {
    const element = document.getElementById('toast');
    element.textContent = text; element.classList.add('visible');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => element.classList.remove('visible'), 2800);
  }

  function drawGraphs() {
    for (const graph of document.querySelectorAll('[data-graph]')) {
      const rect = graph.getBoundingClientRect();
      if (!rect.width || !rect.height) continue;
      const vertical = graph.dataset.graph === 'vertical';
      const nodes = new Map([...graph.querySelectorAll('[data-graph-node]')].map(element => [element.dataset.graphNode, element.getBoundingClientRect()]));
      const svg = graph.querySelector('.graph-lines');
      svg.setAttribute('viewBox', `0 0 ${rect.width} ${rect.height}`);
      svg.innerHTML = current().nodes.flatMap(node => node.deps.map(id => {
        const from = nodes.get(id); const to = nodes.get(node.id);
        if (!from || !to) return '';
        const x1 = (vertical ? from.left + from.width / 2 : from.right) - rect.left;
        const y1 = (vertical ? from.bottom : from.top + from.height / 2) - rect.top;
        const x2 = (vertical ? to.left + to.width / 2 : to.left) - rect.left;
        const y2 = (vertical ? to.top : to.top + to.height / 2) - rect.top;
        const mid = vertical ? (y1 + y2) / 2 : (x1 + x2) / 2;
        const path = vertical ? `M${x1} ${y1} C${x1} ${mid},${x2} ${mid},${x2} ${y2}` : `M${x1} ${y1} C${mid} ${y1},${mid} ${y2},${x2} ${y2}`;
        const state = node.status === 'completed' ? 'done' : node.status === 'running' ? 'live' : '';
        return `<path d="${path}" class="${state}"/>`;
      })).join('');
    }
  }

  function render() {
    const s = current(); const design = window.CTflowVariants[active];
    const prompt = document.getElementById('prompt');
    const focused = document.activeElement === prompt;
    const selection = focused ? [prompt.selectionStart, prompt.selectionEnd] : null;
    const scroll = new Map((renderedDesign === active ? [...stage.querySelectorAll('[data-scroll]')] : []).map(element => [element.dataset.scroll, {
      top: element.scrollTop, bottom: element.scrollHeight - element.scrollTop - element.clientHeight < 35,
    }]));
    document.getElementById('variants').innerHTML = designs.map((id, index) => {
      const item = window.CTflowVariants[id];
      const shapes = ['▰', '▥', '⌘', '▤', '◫'];
      return `<button class="variant-tab ${id === active ? 'selected' : ''}" data-variant="${id}" aria-pressed="${id === active}"><span class="variant-index">0${index + 1}</span><span><strong>${item.label}</strong><small>${e(item.name)}</small></span><span class="variant-shape" aria-hidden="true">${shapes[index]}</span></button>`;
    }).join('');
    document.getElementById('appbar').innerHTML = `<div class="appbar"><span class="app-brand"><span class="brand-mark">⌁</span>ctflow</span><span class="app-separator">/</span><span class="app-project">${icon('git', 12)} E:\\CTflow <span class="app-separator">/</span><span>${s.nodes.length} nodes</span></span><div class="appbar-right"><button class="plain-button" data-action="scenario"><span class="badge ${s.scenario === 'failure' ? 'bad' : s.scenario === 'checkpoint' ? 'warm' : ''}">${s.scenario === 'failure' ? '故障演练' : s.scenario === 'checkpoint' ? '人工检查' : '自由试玩'}</span></button><button class="plain-button worker-count" data-action="workers">${icon('layers', 13)} ${s.workers.length} Workers</button><button class="plain-button model-pick" data-action="models">${e(s.model)} ${icon('chevron-down', 11)}</button><button class="icon-button" data-action="help" aria-label="命令与快捷键">${icon('help', 15)}</button></div></div>`;
    stage.innerHTML = design.render(s, C);
    renderedDesign = active;
    const scrollRegions = '.focus-scroll,.rail-chat-scroll,.rail-graph-scroll,.rail-inspector-scroll,.canvas-map,.canvas-inspector-content,.pulse-timeline,.pulse-events-scroll,.pulse-mini-graph,.pulse-inspector,.studio-inspector,.studio-document,.variant-studio';
    stage.querySelectorAll(scrollRegions).forEach((element, index) => { element.dataset.scroll = `${active}-region-${index}`; });
    document.getElementById('statusbar').innerHTML = `<span class="status-live ${s.status}" role="status">${D.stateLabels[s.status]}</span><span>${s.nodes.filter(n => n.status === 'completed').length}/${s.nodes.length} 节点</span><span>${s.tokens.toLocaleString()} 模拟 tok</span><span>${s.elapsed.toFixed(1)}s</span><span class="status-hints">/ 命令 &nbsp; ↑↓ 选择 &nbsp; Tab 补全 &nbsp; Shift+Tab 模式 &nbsp; Esc 暂停</span>`;
    document.getElementById('design-intent').textContent = `${design.label} / ${design.subtitle}`;
    document.title = `CTflow · ${design.name} / ${design.label}`;
    for (const element of stage.querySelectorAll('[data-scroll]')) {
      const previous = scroll.get(element.dataset.scroll);
      const follow = element.dataset.scroll === 'chat' || element.dataset.scroll.startsWith('activity-') || element.matches('.focus-scroll,.rail-chat-scroll,.pulse-events-scroll');
      if (!previous) element.scrollTop = follow && s.messages.length ? element.scrollHeight : 0;
      else element.scrollTop = follow && previous.bottom ? element.scrollHeight : previous.top;
    }
    if (focused && !overlay) {
      const input = document.getElementById('prompt'); input.focus({ preventScroll: true });
      input.setSelectionRange(...selection);
    }
    updatePalette();
    requestAnimationFrame(drawGraphs);
  }

  function suggestions() {
    const text = current().draft;
    const command = /^\/([^\s]*)$/.exec(text);
    if (command) return commandList.filter(item => item.name.slice(1).includes(command[1].toLowerCase())).map(item => ({ ...item, value: item.name + (item.args ? ' ' : '') }));
    const mention = /(?:^|\s)@([\w-]*)$/.exec(text);
    if (mention) return current().workers.filter(worker => worker.id.includes(mention[1])).map(worker => ({ name: `@${worker.id}`, detail: worker.name, group: 'Worker', value: `@${worker.id} `, mention: true }));
    return [];
  }

  function updatePalette() {
    const palette = document.getElementById('palette');
    if (!palette) return;
    const input = current().draft;
    const eligible = /^\/[^\s]*$/.test(input) || /(?:^|\s)@[\w-]*$/.test(input);
    if (!eligible || paletteDismissed) { palette.hidden = true; return; }
    const items = suggestions();
    paletteIndex = Math.max(0, Math.min(paletteIndex, items.length - 1));
    palette.hidden = false;
    palette.innerHTML = `<div class="palette-heading"><span>${input.includes('@') ? '选择 Worker' : '你想做什么？'}</span><span>↑↓ 选择 · Tab 补全 · Enter 执行</span></div><div class="palette-options" role="listbox" aria-label="命令建议">${items.length ? items.map((item, index) => `<button class="palette-option ${index === paletteIndex ? 'selected' : ''}" role="option" aria-selected="${index === paletteIndex}" data-suggestion="${index}"><code>${e(item.name)}</code><span>${e(item.detail)}</span><small>${e(item.group)}</small></button>`).join('') : '<div class="palette-empty">没有匹配的命令。试试 /help。</div>'}</div>`;
    palette.querySelector('.palette-option.selected')?.scrollIntoView({ block: 'nearest' });
  }

  function focusInput(text) {
    if (text !== undefined) current().draft = text;
    const input = document.getElementById('prompt');
    input.value = current().draft; input.focus({ preventScroll: true });
    input.setSelectionRange(input.value.length, input.value.length);
    paletteDismissed = false; paletteIndex = 0; updatePalette();
  }

  function chooseSuggestion(index, execute) {
    const item = suggestions()[index];
    if (!item) return;
    if (item.mention) { focusInput(current().draft.replace(/@[\w-]*$/, item.value)); return; }
    if (execute && !item.args) { executeCommand(item.name); return; }
    focusInput(item.value);
  }

  function switchDesign(id) {
    active = id; paletteIndex = 0; paletteDismissed = false; historyIndex = -1;
    location.hash = id; closeOverlay(false); render(); save();
  }

  function showOverlay(type, data = '') {
    overlay = { type, data }; renderOverlay();
    document.getElementById('lab').inert = true;
    requestAnimationFrame(() => overlayRoot.querySelector('input,textarea,select,button')?.focus({ preventScroll: true }));
  }

  function closeOverlay(focus = true) {
    overlay = null; overlayRoot.innerHTML = ''; document.getElementById('lab').inert = false;
    if (focus) document.getElementById('prompt')?.focus({ preventScroll: true });
  }

  function renderOverlay() {
    if (!overlay) return;
    const s = current(); let title; let body; let wide = false;
    const scenarioSelect = `<label class="scenario-picker">运行场景<select data-scenario aria-label="运行场景" ${D.busy(s) ? 'disabled' : ''}><option value="normal" ${s.scenario === 'normal' ? 'selected' : ''}>正常执行</option><option value="failure" ${s.scenario === 'failure' ? 'selected' : ''}>失败 → 重试</option><option value="checkpoint" ${s.scenario === 'checkpoint' ? 'selected' : ''}>人工检查 → 批准</option></select></label>`;
    switch (overlay.type) {
      case 'guide':
        title = '五种布局，同一个可玩闭环';
        body = `<p>选择上方任一版本，在输入区描述一个目标。每版有独立的试玩状态，随时切换比较。</p><div class="guide-grid"><div class="guide-item"><span class="guide-number">01 / COMPOSE</span><h3>从一句话开始</h3><p>输入目标，或选择一份示例。Plan 先给方案；Shift+Tab 切换 Build，提交目标后直接运行。文字中的“串行”可以改变依赖。</p></div><div class="guide-item"><span class="guide-number">02 / COMMAND</span><h3>一条输入线完成操作</h3><p>输入 / 筛选命令，↑↓ 选择，Tab 补全，Enter 执行。输入 @ 选择 Worker；/add con after A,B 可加入节点。</p></div><div class="guide-item"><span class="guide-number">03 / INSPECT</span><h3>跟着结果，检查交接</h3><p>运行后点击节点，切换结果、上下文和活动。/pause 保留进度，/resume 继续。人工检查场景用 /approve 批准成稿。</p></div><div class="guide-item"><span class="guide-number">04 / RECOVER</span><h3>让中断也有清楚的去向</h3><p>失败场景会让风险节点超时。/retry 复用成功节点，完成后打开成稿、复制 Markdown 或导出。</p></div></div><div class="guide-footer"><button class="primary-button" data-action="close-overlay">开始试玩 ${icon('arrow', 13)}</button>${scenarioSelect}</div><p style="margin:18px 0 0;font-size:10px;color:var(--dim)">这是浏览器中的 TUI 交互原型。编排、节点执行、Token 和成稿均为本地模拟；会话保存在本浏览器。设计参考 <a href="https://docs.x.ai/build/modes-and-commands" target="_blank" rel="noreferrer" style="color:var(--accent)">Grok Build</a> 与 <a href="https://opencode.ai/docs/themes/" target="_blank" rel="noreferrer" style="color:var(--accent)">OpenCode</a>。</p>`;
        break;
      case 'help':
        title = '命令与快捷键';
        body = `<p>命令统一作用于当前版本。带参数的命令会先放进输入区。</p><div class="help-commands">${commandList.map(item => `<div class="help-command"><button data-pick-command="${e(item.name)}">${e(item.name)}</button><span>${e(item.detail)}${item.hint ? `<br><small>${e(item.hint)}</small>` : ''}</span></div>`).join('')}</div><p style="margin-top:18px;font-size:10px">Enter 发送 · Shift+Enter 换行 · Shift+Tab 切换模式 · Ctrl+K / Ctrl+P 命令 · Esc 关闭弹层 / 暂停</p>`;
        break;
      case 'flow':
        title = `${s.workflowName} / FLOW`; wide = true;
        body = `${C.graph(s)}<div class="guide-footer">${C.runControls(s)}<span class="text-muted" style="font-size:11px">选择节点，深入检查</span></div>`;
        break;
      case 'inspector':
        title = '节点 / INSPECT'; body = C.inspector(s);
        break;
      case 'artifact':
        title = '成稿 / ARTIFACT'; wide = true;
        body = `<div style="height:min(65dvh,620px);min-height:330px">${C.artifact(s)}</div>`;
        break;
      case 'workers':
        title = '装配一支团队'; wide = true;
        body = `<p>Worker 是可复用的装配方案。调整名称、指令或模型，再把它加入当前流程。</p><div class="worker-grid">${s.workers.map(worker => `<article class="worker-card"><h3>${e(worker.name)}</h3><small>@${e(worker.id)}</small><p>${e(worker.prompt)}</p><div class="worker-card-footer"><span>${e(worker.model)} · ${worker.tools.length} tools</span><button class="plain-button" data-worker="${e(worker.id)}">配置 ${icon('arrow', 12)}</button></div></article>`).join('')}</div>`;
        break;
      case 'worker': {
        const worker = s.workers.find(w => w.id === overlay.data); title = `Worker / @${worker.id}`;
        body = `<form id="worker-form" data-worker-id="${worker.id}"><label class="field-label" for="worker-name">名称</label><input class="field-control" id="worker-name" name="name" value="${e(worker.name)}" required maxlength="20"><label class="field-label" for="worker-model">模型</label><select class="field-control" id="worker-model" name="model">${['qwen-plus', 'qwen-max', 'grok-build', 'gpt-6.1-sol'].map(model => `<option ${model === worker.model ? 'selected' : ''}>${model}</option>`).join('')}</select><label class="field-label" for="worker-prompt">指令</label><textarea class="field-control" id="worker-prompt" name="prompt" required>${e(worker.prompt)}</textarea><p style="font-size:11px;color:var(--muted)">Skill · ${e(worker.skill)} &nbsp; / &nbsp; Tools · ${e(worker.tools.join('、') || '无')}</p><div class="form-actions"><button type="button" class="secondary-button" data-add-worker="${worker.id}" ${D.busy(s) || s.status !== 'idle' ? 'disabled' : ''}>加入流程</button><button type="submit" class="primary-button" ${D.busy(s) ? 'disabled' : ''}>保存装配</button></div></form>`;
        break;
      }
      case 'models':
        title = '选择模型 / MODEL';
        body = `<p>改变团队默认模型，观察装配与上下文信息的更新。此处模型选择用于交互演示。</p>${[['qwen-plus', '平衡质量与速度'], ['qwen-max', '深入推敲复杂问题'], ['grok-build', '面向构建与工具任务'], ['gpt-6.1-sol', '推理、实现与交付']].map(([model, detail]) => `<button class="model-option ${model === s.model ? 'selected' : ''}" data-model="${model}">${icon(model === s.model ? 'check' : 'brain', 15)}<span>${model}<small>${detail}</small></span></button>`).join('')}`;
        break;
      case 'sessions':
        title = '本版运行历史 / RUNS';
        body = s.history.length ? `<p>选择一次运行，查看当时的节点结果与成稿。</p>${s.history.map((run, index) => `<button class="session-option" data-session="${index}">${icon('history', 17)}<span>${e(run.name)}<small>${run.id} · ${run.tokens.toLocaleString()} tok · ${run.elapsed.toFixed(1)}s</small></span><span class="badge ${run.status === 'completed' ? 'good' : run.status === 'failed' ? 'bad' : ''}">${D.stateLabels[run.status]}</span></button>`).join('')}` : '<div class="session-empty">先完成一次运行。成功、失败和停止的记录都会保留在这里。</div>';
        break;
      case 'scenario':
        title = '给这次试玩加一点变化';
        body = `<p>切换下一次运行的场景，体验流程怎样回应中断和人工决策。</p>${[['normal', '顺畅执行', '五个节点按依赖推进，完成后生成报告。'], ['failure', '一次失败，继续向前', '风险节点模拟超时；重试时复用成功结果。'], ['checkpoint', '成稿前，留一个决定', '分析完成后暂停，由你检查依据并批准成稿。']].map(([value, name, detail]) => `<button class="model-option ${s.scenario === value ? 'selected' : ''}" data-scenario-choice="${value}" ${D.busy(s) ? 'disabled' : ''}>${icon(s.scenario === value ? 'check' : value === 'normal' ? 'play' : value === 'failure' ? 'history' : 'eye', 16)}<span>${name}<small>${detail}</small></span></button>`).join('')}<p style="margin:18px 0 0;font-size:11px">${D.busy(s) ? '当前流程进行中。完成或停止后，可以切换场景。' : '选择后，点击“运行方案”或输入 /run。'}</p>`;
        break;
      default: throw new Error(`未知弹层：${overlay.type}`);
    }
    overlayRoot.innerHTML = `<div class="overlay-backdrop" data-backdrop><section class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true" aria-labelledby="modal-title"><header class="modal-header"><h2 id="modal-title">${e(title)}</h2><button class="icon-button" data-action="close-overlay" aria-label="关闭弹层">${icon('close', 17)}</button></header><div class="modal-body">${body}</div></section></div>`;
    requestAnimationFrame(drawGraphs);
  }

  function exportArtifact() {
    const s = current();
    if (!s.nodes.some(node => node.output)) throw new Error('还没有可导出的结果。先运行一次方案。');
    const url = URL.createObjectURL(new Blob([s.artifact || D.markdown(s)], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url; link.download = `ctflow-${active}-${s.runId}.md`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000); toast('已导出本次试玩的 Markdown');
  }

  function executeCommand(text) {
    const s = current(); const value = text.trim();
    if (!value) return;
    s.commandHistory.push(value); s.commandHistory = s.commandHistory.slice(-60);
    s.draft = ''; paletteDismissed = true; historyIndex = -1;
    try {
      if (!value.startsWith('/')) {
        D.plan(s, value);
        if (s.mode === 'build') D.run(s);
      } else {
        const [, command, raw = ''] = /^\/(\S+)\s*([\s\S]*)$/.exec(value) || [];
        const argument = raw.trim();
        switch (command?.toLowerCase()) {
          case 'plan':
            if (!argument) { s.mode = 'plan'; render(); focusInput('/plan '); toast('写下目标，或点一份示例'); return; }
            s.mode = 'plan'; D.plan(s, argument); break;
          case 'run': s.mode = 'build'; D.run(s); break;
          case 'pause': D.pause(s); break;
          case 'resume': D.resume(s); break;
          case 'approve': D.approve(s); break;
          case 'stop': D.stop(s); break;
          case 'retry': D.retry(s); break;
          case 'flow': showOverlay('flow'); break;
          case 'artifact': showOverlay('artifact'); break;
          case 'workers': showOverlay('workers'); break;
          case 'model': showOverlay('models'); break;
          case 'sessions': showOverlay('sessions'); break;
          case 'scenario': showOverlay('scenario'); break;
          case 'help': showOverlay('help'); break;
          case 'inspect':
            if (argument) {
              if (!s.nodes.some(node => node.id === argument.toUpperCase())) throw new Error('节点不存在。请使用画布中的节点 ID，例如 /inspect A。');
              s.selectedNode = argument.toUpperCase();
            }
            showOverlay('inspector'); break;
          case 'add': {
            const match = /^(\w+)(?:\s+after\s+([\w,\s]+))?$/i.exec(argument);
            if (!match) { render(); focusInput('/add '); toast('例如 /add con after A,B'); return; }
            D.addNode(s, match[1], match[2]); toast('节点已加入流程'); break;
          }
          case 'export': exportArtifact(); break;
          case 'new': {
            if (D.busy(s)) throw new Error('先用 /stop 结束当前流程，再开始新会话。');
            const next = D.makeState(); next.history = s.history; next.runNumber = s.runNumber; next.scenario = s.scenario;
            states[active] = next; toast('新会话已就绪'); break;
          }
          default: throw new Error('没有这个命令。输入 /help 查看可用操作。');
        }
      }
    } catch (error) { D.message(current(), 'error', error.message); toast(error.message); }
    render(); save();
    if (overlay) renderOverlay();
  }

  function action(name) {
    const s = current();
    switch (name) {
      case 'send': executeCommand(document.getElementById('prompt').value); break;
      case 'focus': focusInput(); break;
      case 'palette': focusInput('/'); break;
      case 'mode': s.mode = s.mode === 'plan' ? 'build' : 'plan'; render(); save(); toast(s.mode === 'plan' ? 'Plan · 提交目标后先检查方案' : 'Build · 提交目标后直接运行'); break;
      case 'guide': case 'help': case 'workers': case 'sessions': case 'models': case 'scenario': case 'inspector': case 'artifact': showOverlay(name); break;
      case 'close-overlay': closeOverlay(); break;
      case 'copy': navigator.clipboard.writeText(s.artifact || D.markdown(s)).then(() => toast('Markdown 已复制')).catch(error => toast(`复制失败：${error.message}`)); break;
      default: throw new Error(`未接入操作：${name}`);
    }
  }

  document.addEventListener('click', event => {
    if (event.target.matches('[data-backdrop]')) { closeOverlay(); return; }
    const target = event.target.closest('button');
    if (!target || target.disabled) return;
    const data = target.dataset; const s = current();
    if (data.variant) { switchDesign(data.variant); return; }
    if (data.suggestion !== undefined) { chooseSuggestion(Number(data.suggestion), true); return; }
    if (data.pickCommand) {
      const command = commandList.find(item => item.name === data.pickCommand); closeOverlay();
      if (command.args) focusInput(`${command.name} `); else executeCommand(command.name);
      return;
    }
    if (data.command) { executeCommand(data.command); return; }
    if (data.insert !== undefined) { closeOverlay(false); focusInput(data.insert); return; }
    if (data.template) {
      try { D.plan(s, D.templates[data.template].task, data.template); render(); save(); focusInput(); }
      catch (error) { toast(error.message); }
      return;
    }
    if (data.node) {
      s.selectedNode = data.node; s.activeTab = 'output'; render(); save();
      if (overlay?.type === 'flow' || data.openNode) showOverlay('inspector');
      else if (overlay) renderOverlay();
      else if (active === 'focus') showOverlay('inspector');
      return;
    }
    if (data.tab) { s.activeTab = data.tab; render(); if (overlay) renderOverlay(); save(); return; }
    if (data.artifactTab) { s.artifactTab = data.artifactTab; render(); if (overlay) renderOverlay(); save(); return; }
    if (data.worker) { showOverlay('worker', data.worker); return; }
    if (data.addWorker) {
      try { D.addNode(s, data.addWorker); closeOverlay(); render(); save(); toast('已加入流程，默认衔接最后一个节点'); }
      catch (error) { toast(error.message); }
      return;
    }
    if (data.model) {
      if (D.busy(s)) { toast('当前流程进行中，停止或完成后可切换模型'); return; }
      s.model = data.model; s.workers.forEach(worker => { worker.model = data.model; });
      closeOverlay(); render(); save(); toast(`团队默认模型已切换为 ${data.model}`); return;
    }
    if (data.scenarioChoice) { s.scenario = data.scenarioChoice; closeOverlay(); render(); save(); toast('演示场景已切换'); return; }
    if (data.session !== undefined) {
      if (D.busy(s)) { toast('当前流程进行中，停止或完成后可查看历史快照'); return; }
      const run = structuredClone(s.history[Number(data.session)]);
      Object.assign(s, { nodes: run.nodes, status: run.status, tokens: run.tokens, elapsed: run.elapsed, task: run.task, artifact: run.artifact, logs: run.logs, workflowName: run.name, runId: run.id, template: run.template, selectedNode: run.nodes.at(-1).id, activeTab: 'output', messages: [], failureTriggered: run.status === 'failed' });
      D.message(s, 'system', `已打开 ${run.id} 的运行记录。`); closeOverlay(); render(); save(); return;
    }
    if (data.action) action(data.action);
  });

  document.addEventListener('input', event => {
    if (event.target.id !== 'prompt') return;
    current().draft = event.target.value; paletteDismissed = false; paletteIndex = 0; historyIndex = -1; updatePalette();
  });

  document.addEventListener('change', event => {
    if (event.target.matches('[data-scenario]')) { current().scenario = event.target.value; save(); render(); toast('演示场景已切换'); }
  });

  document.addEventListener('submit', event => {
    if (event.target.id !== 'worker-form') return;
    event.preventDefault();
    const s = current(); if (D.busy(s)) { toast('当前流程进行中，完成后可调整装配'); return; }
    const data = new FormData(event.target); const worker = s.workers.find(w => w.id === event.target.dataset.workerId);
    worker.name = data.get('name').trim(); worker.model = data.get('model'); worker.prompt = data.get('prompt').trim();
    if (!worker.name || !worker.prompt) { toast('请填写名称与指令'); return; }
    s.nodes.filter(node => node.worker === worker.id).forEach(node => { node.label = worker.name; });
    closeOverlay(); render(); save(); toast(`@${worker.id} 装配已保存`);
  });

  document.addEventListener('keydown', event => {
    if (event.isComposing) return;
    if (overlay) {
      if (event.key === 'Escape') { event.preventDefault(); closeOverlay(); return; }
      if (event.key === 'Tab') {
        const items = [...overlayRoot.querySelectorAll('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),a[href]')];
        const first = items[0]; const last = items.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
      return;
    }
    const input = document.getElementById('prompt'); const inPrompt = event.target === input;
    if ((event.ctrlKey || event.metaKey) && ['k', 'p'].includes(event.key.toLowerCase())) { event.preventDefault(); focusInput('/'); return; }
    if (event.shiftKey && event.key === 'Tab') { event.preventDefault(); action('mode'); return; }
    if (event.key === 'Escape') {
      event.preventDefault();
      const palette = document.getElementById('palette');
      if (!palette.hidden) { paletteDismissed = true; updatePalette(); }
      else if (current().status === 'running') executeCommand('/pause');
      else { focusInput(''); }
      return;
    }
    if (!inPrompt && event.key === '/') { event.preventDefault(); focusInput('/'); return; }
    if (!inPrompt && event.key === '?') { event.preventDefault(); showOverlay('guide'); return; }
    if (!inPrompt) return;
    const palette = document.getElementById('palette');
    if (!palette.hidden && suggestions().length) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); const count = suggestions().length;
        paletteIndex = (paletteIndex + (event.key === 'ArrowDown' ? 1 : count - 1)) % count; updatePalette(); return;
      }
      if (event.key === 'Tab') { event.preventDefault(); chooseSuggestion(paletteIndex, false); return; }
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault(); chooseSuggestion(paletteIndex, true); return;
      }
    }
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); executeCommand(input.value); return; }
    if (event.key === 'ArrowUp' && (input.value === '' || historyIndex >= 0) && !event.shiftKey) {
      const history = current().commandHistory;
      if (!history.length) return;
      event.preventDefault(); historyIndex = Math.min(history.length - 1, historyIndex + 1); focusInput(history.at(-1 - historyIndex));
    } else if (event.key === 'ArrowDown' && historyIndex >= 0) {
      event.preventDefault(); historyIndex -= 1; focusInput(historyIndex < 0 ? '' : current().commandHistory.at(-1 - historyIndex));
    }
  });

  addEventListener('hashchange', () => {
    const id = location.hash.slice(1); if (designs.includes(id) && id !== active) switchDesign(id);
  });
  addEventListener('pagehide', save);
  new ResizeObserver(drawGraphs).observe(stage);
  new ResizeObserver(drawGraphs).observe(overlayRoot);
  setInterval(() => {
    const now = performance.now(); const seconds = Math.min((now - lastTick) / 1000, .5); lastTick = now;
    let changed = false; let transitioned = false;
    for (const id of designs) {
      const status = states[id].status;
      if (D.advance(states[id], seconds)) { if (id === active) changed = true; }
      if (status !== states[id].status) transitioned = true;
    }
    if (changed) { render(); if (overlay && ['flow', 'inspector', 'artifact'].includes(overlay.type)) renderOverlay(); }
    if (transitioned || (now - lastSave > 1200 && Object.values(states).some(s => s.status === 'running'))) { save(); lastSave = now; }
  }, 200);
  render();
})();
