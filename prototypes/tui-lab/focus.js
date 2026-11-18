(function () {
  'use strict';

  window.CTflowVariants.focus = {
    name: '留白',
    subtitle: '把想法留在中央，让流程随对话展开。',
    label: 'MONO',
    render(s, c) {
      const statusNames = {
        idle: s.messages.length ? '方案已就绪' : '等待你的想法', running: '正在构建', paused: '已暂停',
        review: '等待你的判断', failed: '需要处理', completed: '构建完成', stopped: '已停止'
      };
      const completed = s.nodes.filter(node => node.status === 'completed').length;
      const active = s.nodes.filter(node => node.status === 'running');
      const fresh = s.status === 'idle' && s.messages.length === 0;
      const attention = s.status === 'review' || s.status === 'failed';
      const activityLine = active.length
        ? active.map(node => node.name).join(' · ')
        : s.status === 'completed' ? '所有节点已完成，产物可以打开。'
          : s.status === 'review' ? '在继续之前，查看节点结果并做出判断。'
            : s.status === 'failed' ? '查看失败节点的活动记录，然后重试。'
              : s.status === 'paused' ? '上下文已保留，随时可以继续。'
                : s.status === 'stopped' ? '本次运行已停止，可以重新规划。'
                  : '你的方案将在这里成为可执行的流程。';

      return `<div class="variant-focus ${fresh ? 'focus-fresh' : 'focus-session'}">
        <div class="focus-scroll">
          <div class="focus-column">
            <div class="focus-session-line">
              <span class="focus-session-label">${c.icon('terminal', 14)} PRIVATE WORKSPACE</span>
              <button class="plain-button" data-action="sessions">${c.icon('history', 14)} 会话</button>
            </div>
            ${fresh ? `<section class="focus-welcome" aria-label="开始新的构建">
              <div class="focus-wordmark" aria-label="CTflow">CT<span>_</span>flow<span class="focus-cursor">▌</span></div>
              <p class="focus-welcome-kicker">THINK IT. BUILD IT. REFINE IT.</p>
              <h1>从一个想法开始。</h1>
              <p class="focus-intro">说出你想完成的事。一起拆解、并行探索，直到作品成形。</p>
              <div class="focus-starters">
                <button data-template="research">${c.icon('search', 16)} 深入一个问题 ${c.icon('arrow', 14)}</button>
                <button data-template="review">${c.icon('git', 16)} 审视一个方案 ${c.icon('arrow', 14)}</button>
                <button data-template="launch">${c.icon('bolt', 16)} 把想法做出来 ${c.icon('arrow', 14)}</button>
              </div>
            </section>` : `<header class="focus-task">
              <span class="eyebrow">CURRENT SESSION / ${c.e(s.mode.toUpperCase())}</span>
              <h1>${c.e(s.task || s.workflowName || '新的构建')}</h1>
              <p>${c.e(s.workflowName)} <span>·</span> ${c.e(s.model)}</p>
            </header>`}
            <div class="focus-conversation">${c.chat(s, { welcome: false })}</div>
            ${!fresh ? `<section class="focus-workflow ${attention ? 'focus-attention' : ''}" aria-label="会话执行流程">
              <div class="focus-workflow-head">
                <div class="focus-run-title"><span class="focus-status-dot" data-status="${c.e(s.status)}"></span><strong>${c.e(statusNames[s.status])}</strong><span class="focus-count">${completed}/${s.nodes.length}</span></div>
                ${c.runControls(s)}
              </div>
              <p class="focus-activity-line">${c.e(activityLine)}</p>
              <div class="focus-progress">${c.progress(s)}</div>
              <details class="focus-flow-details" ${attention || s.status === 'running' ? 'open' : ''}>
                <summary><span>${c.icon('workflow', 14)} 执行流程</span><span>展开节点与依赖 ${c.icon('chevron-down', 13)}</span></summary>
                <div class="focus-flow-body">
                  <div class="focus-graph">${c.graph(s, { orientation: 'horizontal', compact: true })}</div>
                  <div class="focus-node-strip">${c.nodeList(s, { compact: true })}</div>
                  <div class="focus-flow-actions"><button class="plain-button" data-action="inspector">${c.icon('eye', 14)} 深入所选节点 ${c.e(s.selectedNode || '')}</button><button class="plain-button" data-action="workers">${c.icon('layers', 14)} Worker 装配</button></div>
                </div>
              </details>
              <div class="focus-result-line"><span>${c.metrics(s)}</span><button class="plain-button" data-action="artifact">${c.icon('file', 14)} ${s.status === 'completed' ? '打开最终产物' : '查看工作产物'} ${c.icon('arrow', 13)}</button></div>
            </section>` : ''}
          </div>
        </div>
        <footer class="focus-input-dock">
          <div class="focus-input-column">
            ${c.composer(s, { placeholder: fresh ? '你想构建什么？' : '继续描述想法，或输入 / 查看命令' })}
            <div class="focus-input-caption"><span>${c.icon('command', 12)} / 命令 <span class="focus-caption-separator">·</span> Plan 思考，Build 执行</span><button class="plain-button" data-action="help">快捷键 ${c.icon('help', 12)}</button></div>
          </div>
        </footer>
      </div>`;
    }
  };
}());
