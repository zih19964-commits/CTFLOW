(function () {
  'use strict';
  const D = window.CTflowDemo;
  const e = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  const paths = {
    terminal: '<path d="m4 6 5 5-5 5m8 1h7"/>', plus: '<path d="M12 5v14M5 12h14"/>',
    chevron: '<path d="m9 5 7 7-7 7"/>', 'chevron-down': '<path d="m6 9 6 6 6-6"/>',
    arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>', play: '<path d="m8 4 12 8-12 8Z"/>',
    pause: '<path d="M8 5v14M16 5v14"/>', stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    check: '<path d="m5 12 4 4L19 6"/>', close: '<path d="m6 6 12 12M6 18 18 6"/>',
    code: '<path d="m8 7-5 5 5 5m8-10 5 5-5 5m-3-13-2 16"/>',
    search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
    settings: '<path d="m9 3 1-1h4l1 3 3 1 3 4-2 2 1 3-3 3-3-1-2 3H8l-1-3-3-1-2-4 2-2-1-3 3-3 3 1Z"/><circle cx="12" cy="11" r="3"/>',
    history: '<path d="M3 11a9 9 0 1 1 2 7M3 4v7h7m2-4v5l3 2"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    git: '<circle cx="6" cy="5" r="2"/><circle cx="18" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><path d="M6 7v10m12-10a10 10 0 0 1-10 7H6"/>',
    workflow: '<rect x="2" y="9" width="6" height="6" rx="1"/><rect x="16" y="2" width="6" height="6" rx="1"/><rect x="16" y="16" width="6" height="6" rx="1"/><path d="M8 12h4V5h4m-4 7v7h4"/>',
    bolt: '<path d="m13 2-9 12h7l-1 8L21 9h-8Z"/>',
    file: '<path d="M14 2H5v20h14V7Zm0 0v6h5M8 12h8m-8 4h6"/>',
    copy: '<rect x="8" y="8" width="12" height="13" rx="1"/><path d="M16 8V3H3v13h5"/>',
    expand: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m8 0h5v-5"/>',
    command: '<path d="M8 8h8v8H8ZM8 8V5a3 3 0 1 0-3 3Zm8 0h3a3 3 0 1 0-3-3Zm0 8v3a3 3 0 1 0 3-3Zm-8 0H5a3 3 0 1 0 3 3Z"/>',
    sliders: '<path d="M4 6h16M4 12h16M4 18h16M8 3v6m8 0v6m-6 0v6"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9 8a3 3 0 1 1 4 3c-1 .5-1 1-1 2m0 3h.01"/>',
    eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
    brain: '<path d="M12 4c-3-4-8-1-7 3-4 1-4 7 0 8-1 5 5 8 7 4m0-15c3-4 8-1 7 3 4 1 4 7 0 8 1 5-5 8-7 4Zm-4 3 4 3 4-3m-9 8 5-3 5 3"/>',
    layers: '<path d="m12 2 10 6-10 6L2 8Zm-9 11 9 5 9-5M3 18l9 5 9-5"/>',
  };
  const icon = (name, size = 16) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.terminal}</svg>`;

  function layers(s) {
    const done = new Set(); const result = [];
    while (done.size < s.nodes.length) {
      const row = s.nodes.filter(node => !done.has(node.id) && node.deps.every(id => done.has(id)));
      if (!row.length) throw new Error('演示工作流存在循环依赖。');
      result.push(row); row.forEach(n => done.add(n.id));
    }
    return result;
  }

  function runControls(s) {
    const button = (command, label, glyph, primary = false) => `<button class="${primary ? 'primary-button' : 'secondary-button'}" data-command="${command}">${icon(glyph, 13)}${label}</button>`;
    if (s.status === 'running') return `<div class="run-controls">${button('/pause', '暂停', 'pause')}${button('/stop', '停止', 'stop')}</div>`;
    if (s.status === 'paused') return `<div class="run-controls">${button('/resume', '继续执行', 'play', true)}${button('/stop', '停止', 'stop')}</div>`;
    if (s.status === 'review') return `<div class="run-controls">${button('/approve', '批准成稿', 'check', true)}${button('/stop', '停止', 'stop')}</div>`;
    if (s.status === 'failed') return `<div class="run-controls">${button('/retry', '重试失败节点', 'history', true)}<button class="plain-button" data-action="inspector">检查失败</button></div>`;
    if (s.status === 'completed') return `<div class="run-controls"><button class="primary-button" data-action="artifact">${icon('file', 13)}打开成稿</button>${button('/run', '再次运行', 'play')}</div>`;
    return `<div class="run-controls">${button('/run', s.status === 'stopped' ? '重新运行' : '运行方案', 'play', true)}<button class="plain-button" data-action="scenario">${icon('sliders', 13)}${s.scenario === 'failure' ? '失败重试' : s.scenario === 'checkpoint' ? '人工检查' : '演示场景'}</button></div>`;
  }

  function progress(s) {
    const done = s.nodes.filter(n => n.status === 'completed').length;
    const percent = s.nodes.reduce((sum, n) => sum + (n.status === 'completed' ? 100 : n.status === 'running' ? n.progress : 0), 0) / s.nodes.length;
    return `<div class="run-progress" aria-label="${done} / ${s.nodes.length} 节点完成"><div class="progress-track"><span style="width:${percent}%"></span></div><span>${done}/${s.nodes.length}</span></div>`;
  }

  function graph(s, options = {}) {
    const orientation = options.orientation || 'horizontal';
    return `<div class="flow-graph ${orientation} ${options.compact ? 'compact' : ''}" data-graph="${orientation}"><svg class="graph-lines" aria-hidden="true"></svg><div class="flow-layers">${layers(s).map((row, index) => `<div class="flow-layer" data-layer="${index}">${row.map(node => `<button class="flow-node ${node.status} ${s.selectedNode === node.id ? 'selected' : ''}" data-node="${node.id}" data-graph-node="${node.id}" aria-pressed="${s.selectedNode === node.id}" aria-label="${node.id} ${e(node.name)}，${D.nodeLabels[node.status]}"><span class="flow-node-top"><span class="node-id">${node.id}</span><span class="flow-node-status">${node.status === 'completed' ? '✓ ' : ''}${node.status === 'running' ? `${node.progress}%` : D.nodeLabels[node.status]}</span></span><strong class="flow-node-name">${e(node.name)}</strong><span class="flow-node-worker">@${e(node.worker)}</span>${node.status === 'running' ? `<span class="node-progress" style="width:${node.progress}%"></span>` : ''}</button>`).join('')}</div>`).join('')}</div></div>`;
  }

  function nodeList(s, options = {}) {
    return `<div class="node-list ${options.compact ? 'compact' : ''}">${s.nodes.map(node => `<button class="node-row ${node.status} ${node.id === s.selectedNode ? 'selected' : ''}" data-node="${node.id}" aria-pressed="${node.id === s.selectedNode}"><span class="node-symbol">${node.status === 'completed' ? '✓' : node.status === 'failed' ? '!' : node.id}</span><span class="node-info"><strong>${e(node.name)}</strong><small>@${e(node.worker)}</small></span><span class="node-state">${node.status === 'running' ? `${node.progress}%` : D.nodeLabels[node.status]}</span></button>`).join('')}</div>`;
  }

  function activity(s, options = {}) {
    const logs = options.node ? s.logs.filter(item => item.node === options.node) : s.logs;
    return `<div class="activity-log ${options.compact ? 'compact' : ''}" data-scroll="activity-${options.node || 'all'}">${logs.length ? logs.map(item => `<div class="activity-line"><time>${item.time.toFixed(1)}s</time><span class="log-node">${e(item.node)}</span><span class="activity-message">${e(item.text)}</span></div>`).join('') : '<div class="activity-empty">运行后，这里会记录每一次节点交接。</div>'}</div>`;
  }

  function inspector(s, options = {}) {
    const node = s.nodes.find(n => n.id === s.selectedNode);
    if (!node) return '<div class="inspector-empty">选择一个节点，查看它的任务与依据。</div>';
    const worker = s.workers.find(w => w.id === node.worker);
    let body;
    if (s.activeTab === 'context') {
      body = `<p class="eyebrow">本次节点任务</p><p>${e(node.name)}：${e(s.task)}</p><p class="eyebrow">直接上游 · ${node.deps.length} 项</p>${node.deps.length ? node.deps.map(id => { const upstream = s.nodes.find(n => n.id === id); return `<button class="context-tag" data-node="${id}">${id} · ${e(upstream.name)} · ${D.nodeLabels[upstream.status]}</button>`; }).join('') : '<p>独立起点，接收当前总任务。</p>'}<p style="margin-top:16px" class="eyebrow">Worker 指令</p><p>${e(worker.prompt)}</p><span class="context-tag">${e(worker.model)}</span><span class="context-tag">${e(worker.skill)}</span><p style="margin-top:12px;color:var(--dim)">每个节点使用独立上下文，接收直接上游的最终结果。</p>`;
    } else if (s.activeTab === 'activity') body = activity(s, { node: node.id, compact: true });
    else if (node.output) body = `<div class="output-text">${e(node.output)}</div><div class="run-metrics" style="margin-top:18px"><span>${node.tokens.toLocaleString()} 模拟 tok</span><span>${node.started === null ? '—' : `${(node.spent).toFixed(1)}s`}</span></div>`;
    else body = `<div class="inspector-empty"><span>${node.status === 'running' ? `正在${e(node.name)} · ${node.progress}%` : node.status === 'blocked' ? '上游有失败节点。重试完成后将继续。' : '执行结果会出现在这里。'}</span><div class="empty-rule"></div><div class="empty-rule"></div><span>${node.deps.length ? `等待 ${node.deps.join('、')} 的最终结果` : `@${e(node.worker)} · 独立任务上下文`}</span></div>`;
    const heading = options.compact ? '' : `<div class="inspector-title"><span class="node-symbol">${node.id}</span><div><strong>${e(node.name)}</strong><small>@${e(node.worker)} · ${D.nodeLabels[node.status]}</small></div></div>`;
    return `<section class="node-inspector ${options.compact ? 'compact' : ''}">${heading}<div class="inspector-tabs" role="tablist" aria-label="节点详情">${[['output', '结果'], ['context', '上下文'], ['activity', '活动']].map(([key, label]) => `<button role="tab" aria-selected="${s.activeTab === key}" class="${s.activeTab === key ? 'selected' : ''}" data-tab="${key}">${label}</button>`).join('')}</div><div class="inspector-body" role="tabpanel">${body}</div></section>`;
  }

  function workers(s) {
    return `<div class="worker-strip">${s.workers.map(worker => `<button class="worker-chip" data-worker="${e(worker.id)}"><span class="worker-dot"></span>@${e(worker.id)}</button>`).join('')}</div>`;
  }

  function welcome(options) {
    if (options.welcome === false) return '<div class="activity-empty" style="padding:0">示例方案已就绪。描述新目标，或运行当前流程。</div>';
    return `<div class="welcome"><div class="welcome-wordmark"><span class="slash-logo">⌁</span><span>CTflow<span style="color:var(--dim);font-weight:400">_</span></span></div><h2 class="welcome-tagline">把一件事，交给一支团队。</h2><p class="welcome-description">说出目标，编排思路，检查每一次交接。<br>输入 <span class="accent">/</span>，从这里开始。</p><div class="starter-options"><button class="starter" data-template="review"><span>审阅一个产品方案</span><code>research → judge ↗</code></button><button class="starter" data-template="research"><span>从多个角度研究问题</span><code>parallel research ↗</code></button><button class="starter" data-template="launch"><span>把想法写成发布计划</span><code>idea → artifact ↗</code></button></div></div>`;
  }

  function chat(s, options = {}) {
    const lastPlan = s.messages.findLastIndex(m => m.type === 'plan');
    const html = s.messages.map((item, index) => {
      let extra = '';
      if (item.type === 'plan' && index === lastPlan) extra = `<div class="inline-plan"><div class="plan-title"><span>${icon('workflow', 13)} ${e(s.workflowName)}</span><span class="badge">${s.nodes.length} NODES</span></div><div class="plan-route">${layers(s).map((row, i) => `<span>${String(i + 1).padStart(2, '0')} &nbsp; ${row.map(node => e(node.name)).join(' ＋ ')}</span>`).join('<br>')}</div><div class="plan-actions"><button class="secondary-button" data-command="/flow">${icon('eye', 13)}检查流程</button>${s.status === 'idle' ? '<button class="primary-button" data-command="/run">开始运行 ↗</button>' : `<span>${D.stateLabels[s.status]}</span>`}</div></div>`;
      if (item.type === 'review' && s.status === 'review') extra = `<div style="margin-top:12px">${runControls(s)}</div>`;
      if (item.type === 'completed') extra = '<button class="secondary-button" style="margin-top:12px" data-action="artifact">打开成稿 ↗</button>';
      return `<article class="message ${e(item.role)}"><div class="message-label">${item.role === 'user' ? '你' : item.role === 'assistant' ? '⌁ CTflow' : item.role === 'error' ? '执行中断' : '· SYSTEM'}${item.role === 'assistant' ? '<span style="color:var(--dim)">LOCAL DEMO</span>' : ''}</div><div class="message-content">${e(item.text)}</div>${extra}</article>`;
    }).join('');
    return `<div class="chat-stream ${options.compact ? 'compact' : ''}" data-scroll="chat">${html || welcome(options)}</div>`;
  }

  function composer(s, options = {}) {
    return `<div class="composer ${options.compact ? 'compact' : ''}"><div id="palette" class="command-palette" hidden></div><div class="composer-input-row"><span class="prompt-mark" aria-hidden="true">›</span><label class="sr-only" for="prompt">目标或斜杠命令</label><textarea id="prompt" rows="2" spellcheck="false" autocomplete="off" placeholder="${e(options.placeholder || '描述目标，或输入 / 查看命令…')}">${e(s.draft)}</textarea><button class="send-button" data-action="send" aria-label="发送目标或执行命令">${icon('arrow', 15)}</button></div><div class="composer-footer"><div class="composer-footer-left"><button class="mode-switch" data-action="mode">${icon(s.mode === 'plan' ? 'brain' : 'bolt', 12)}${s.mode === 'plan' ? 'Plan' : 'Build'}${icon('chevron-down', 10)}</button><span class="model-inline">${e(s.model)}</span><button class="plain-button" data-action="palette">/ 命令</button></div><span class="composer-hints">Enter 发送 · ⇧Enter 换行</span></div></div>`;
  }

  function artifact(s) {
    let content;
    const complete = s.status === 'completed';
    const hasOutput = s.nodes.some(n => n.output);
    if (s.artifactTab === 'source') content = `<pre class="artifact-source">${e(s.artifact || (hasOutput ? D.markdown(s) : '# 尚待成稿\n\n运行工作流后，这里会显示可导出的 Markdown。'))}</pre>`;
    else if (complete || hasOutput) content = `<div class="doc-eyebrow">${complete ? 'FINAL REPORT' : 'WORK IN PROGRESS'} / CTflow</div><h1>${e(s.workflowName)}</h1><div class="doc-meta">${s.runId} &nbsp;·&nbsp; ${s.nodes.filter(n => n.status === 'completed').length}/${s.nodes.length} 节点完成 &nbsp;·&nbsp; 本地模拟</div><h2>目标</h2><p>${e(s.task)}</p>${complete ? `<div class="doc-callout">建议先完成核心路径，再用真实使用过程检验可用性。让每一步的结论都能追溯到依据。</div><h2>综合建议</h2><p>${e(s.nodes.filter(n => !s.nodes.some(other => other.deps.includes(n.id))).map(n => n.output).join('\n')).replace(/\n/g, '<br>')}</p>` : '<div class="doc-callout">已有分析正在汇集。最终成稿将在工作流完成后生成。</div>'}<h2>依据与交接</h2>${s.nodes.map(node => `<div class="evidence-row"><span>${node.id} &nbsp; ${e(node.name)}</span><button data-node="${node.id}" data-open-node="true">${D.nodeLabels[node.status]} ↗</button></div>`).join('')}`;
    else if (s.status === 'running' || s.status === 'paused') content = `<div class="artifact-working"><span class="doc-eyebrow">GATHERING EVIDENCE</span><h2>先把思路走通。</h2><p>已完成的分析会逐步汇集到这里。</p>${Array.from({ length: 7 }, () => '<div class="skeleton-line"></div>').join('')}</div>`;
    else content = '<div class="artifact-empty"><div class="file-outline" aria-hidden="true"><span></span><span></span><span></span></div><span class="eyebrow">IDEA → EVIDENCE → ARTIFACT</span><h2>让结论，<br>成为可交付的作品。</h2><p>运行方案后，在这里阅读成稿、追溯依据，或导出 Markdown。</p><button class="secondary-button" data-command="/run">运行当前方案 ↗</button></div>';
    return `<section class="artifact-panel"><div class="artifact-toolbar"><div class="artifact-tabs" role="tablist" aria-label="成稿视图">${[['preview', '预览'], ['source', 'Markdown']].map(([key, label]) => `<button role="tab" aria-selected="${s.artifactTab === key}" data-artifact-tab="${key}" class="${s.artifactTab === key ? 'selected' : ''}">${label}</button>`).join('')}</div><button class="icon-button" data-action="copy" aria-label="复制成稿" ${hasOutput ? '' : 'disabled'}>${icon('copy', 14)}</button><button class="icon-button" data-command="/export" aria-label="导出 Markdown" ${hasOutput ? '' : 'disabled'}>${icon('file', 14)}</button></div><div class="artifact-document" data-scroll="artifact">${content}</div></section>`;
  }

  function timeline(s) {
    const projected = {};
    layers(s).flat().forEach(node => { const start = Math.max(0, ...node.deps.map(id => projected[id].end)); projected[node.id] = { start, end: start + node.duration }; });
    const horizon = Math.max(8, s.elapsed + 1, ...Object.values(projected).map(n => n.end));
    return `<div class="run-timeline"><div class="timeline-ruler"><span>NODE / SECONDS</span><div class="timeline-ticks">${[0, .25, .5, .75, 1].map(p => `<span>${(horizon * p).toFixed(1)}s</span>`).join('')}</div></div>${s.nodes.map(node => { const start = node.started === null ? projected[node.id].start : node.started; const width = node.started === null ? node.duration : node.ended !== null ? node.ended - node.started : s.elapsed - node.started; return `<div class="timeline-row"><button class="timeline-name ${s.selectedNode === node.id ? 'selected' : ''}" data-node="${node.id}"><span>${node.id}</span>${e(node.name)}</button><div class="timeline-track"><span class="timeline-bar ${node.status}" style="left:${start / horizon * 100}%;width:${Math.max(.4, width / horizon * 100)}%">${node.status === 'running' ? `${node.progress}%` : node.status === 'completed' ? '✓' : ''}</span></div></div>`; }).join('')}<p class="timeline-empty-note">${s.status === 'idle' ? '细线表示计划区间，运行后显示演示耗时。' : '绿色已完成 · 杏色运行中 · 灰色待执行'}</p></div>`;
  }

  const metrics = s => `<div class="run-metrics"><span><strong>${s.tokens.toLocaleString()}</strong> tok</span><span><strong>${s.elapsed.toFixed(1)}</strong> s</span></div>`;
  window.CTflowUI = { e, icon, layers, runControls, progress, graph, nodeList, inspector, workers, chat, composer, artifact, timeline, activity, metrics };
})();
