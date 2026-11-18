(function () {
  'use strict';

  window.CTflowVariants.pulse = {
    name: '脉冲',
    subtitle: '从并行时间线读懂执行，把注意力放在下一处需要决策的地方。',
    label: 'PULSE',
    render(s, c) {
      const running = s.nodes.filter((node) => node.status === 'running');
      const blocked = s.nodes.filter((node) => node.status === 'blocked');
      const failed = s.nodes.filter((node) => node.status === 'failed');
      const done = s.nodes.filter((node) => node.status === 'completed').length;
      const selected = s.nodes.find((node) => node.id === s.selectedNode);
      const attention = failed.length ? failed : blocked;
      const stateLabels = {
        idle: 'READY', running: 'LIVE', paused: 'PAUSED', review: 'REVIEW',
        failed: 'FAILED', completed: 'COMPLETE', stopped: 'STOPPED',
      };
      const attentionText = failed.length ? '需要重试'
        : s.status === 'review' ? '等待你的检查'
          : blocked.length ? '等待上游结果' : running.length ? '并行执行中' : s.status === 'completed' ? '全部节点已完成' : '等待下一条指令';
      const focusNodes = attention.length ? attention : running;

      return `<div class="variant-pulse">
        <header class="pulse-command-bar">
          <div class="pulse-identity">
            <span class="pulse-live ${s.status === 'running' ? 'is-live' : ''}"><i></i>${stateLabels[s.status]}</span>
            <h1>${c.e(s.workflowName || '执行指挥台')}</h1>
          </div>
          <div class="pulse-command-actions">${c.runControls(s)}</div>
        </header>
        <div class="pulse-telemetry">
          <div class="pulse-count"><strong>${String(running.length).padStart(2, '0')}</strong><span>执行中</span></div>
          <div class="pulse-count"><strong>${String(done).padStart(2, '0')}<small>/${String(s.nodes.length).padStart(2, '0')}</small></strong><span>节点完成</span></div>
          <div class="pulse-count ${failed.length ? 'has-failure' : ''}"><strong>${String(failed.length + blocked.length).padStart(2, '0')}</strong><span>失败 / 等待上游</span></div>
          <div class="pulse-metrics">${c.metrics(s)}</div>
          <div class="pulse-total-progress">${c.progress(s)}</div>
        </div>
        <div class="pulse-operations">
          <main class="pulse-main">
            <section class="pulse-timeline-section" aria-label="并行执行时间线">
              <div class="pulse-section-heading"><span class="eyebrow">01 / PARALLEL EXECUTION</span><span class="pulse-section-note">横轴时间 · 纵轴节点</span></div>
              <div class="pulse-timeline">${c.timeline(s)}</div>
              <div class="pulse-attention">
                <span class="pulse-attention-title">${c.icon(failed.length ? 'close' : s.status === 'review' ? 'eye' : 'bolt', 14)} ${attentionText}</span>
                <div class="pulse-focus-nodes">${focusNodes.map((node) => `<button class="pulse-node-chip ${node.status === 'running' ? 'is-current' : ''} ${node.status === 'failed' ? 'is-failed' : ''}" data-node="${c.e(node.id)}">${c.e(node.id)}<span>${c.e(node.name)}</span>${node.status === 'running' ? `<small>${node.progress}%</small>` : ''}</button>`).join('')}</div>
                ${s.status === 'review' ? '<button class="plain-button" data-action="inspector">查看节点证据</button>' : ''}
              </div>
            </section>
            <section class="pulse-events" aria-label="节点执行日志">
              <div class="pulse-section-heading"><span class="eyebrow">02 / EVENT STREAM</span><span class="pulse-section-note">${s.logs.length} 条事件</span></div>
              <div class="pulse-events-scroll">${c.activity(s, { compact: false })}</div>
            </section>
          </main>
          <aside class="pulse-watch" aria-label="当前节点监视器">
            <header class="pulse-watch-header"><span class="eyebrow">FOCUS / ${c.e(selected ? selected.id : '—')}</span><button class="icon-button" data-action="inspector" title="展开节点" aria-label="展开节点">${c.icon('expand', 15)}</button></header>
            <div class="pulse-watch-title"><h2>${c.e(selected ? selected.name : '选择节点')}</h2><span>${c.e(selected ? selected.label : '在时间线中查看任务')}</span></div>
            <div class="pulse-inspector">${c.inspector(s, { compact: true })}</div>
            <div class="pulse-routing"><span class="eyebrow">EXECUTION MAP</span><div class="pulse-mini-graph">${c.graph(s, { orientation: 'vertical', compact: true })}</div></div>
            <div class="pulse-watch-actions"><button class="plain-button" data-action="artifact">${c.icon('file', 14)} 产物</button><button class="plain-button" data-action="workers">${c.icon('layers', 14)} Workers</button><button class="icon-button" data-action="sessions" title="运行历史" aria-label="运行历史">${c.icon('history', 14)}</button></div>
          </aside>
        </div>
        <footer class="pulse-console">
          <div class="pulse-console-transcript"><span class="pulse-console-label">${c.icon('terminal', 14)} OPERATOR</span><div class="pulse-chat">${c.chat(s, { compact: true, welcome: false })}</div></div>
          <div class="pulse-composer">${c.composer(s, { placeholder: '向工作流下达指令，输入 / 查看操作…', compact: true })}</div>
        </footer>
      </div>`;
    },
  };
})();
