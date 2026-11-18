(function () {
  'use strict';

  window.CTflowVariants.rail = {
    name: '并行',
    subtitle: '在对话里创造，在执行路线上掌握每一步。',
    label: 'RAIL',
    render(s, c) {
      const stateNames = {
        idle: '准备就绪', running: '并行执行中', paused: '运行已暂停',
        review: '人工检查点', failed: '节点执行失败', completed: '已交付', stopped: '已停止'
      };
      const completeCount = s.nodes.filter(node => node.status === 'completed').length;
      const runningCount = s.nodes.filter(node => node.status === 'running').length;
      const selected = s.nodes.find(node => node.id === s.selectedNode);
      const fresh = s.status === 'idle' && s.messages.length === 0;
      return `<div class="variant-rail">
        <nav class="rail-tools" aria-label="工作台工具">
          <div class="rail-tool-mark" title="CTflow">ct<span>:</span></div>
          <button class="rail-tool rail-tool-active" data-action="focus" title="聚焦对话" aria-label="聚焦对话">${c.icon('terminal', 19)}</button>
          <button class="rail-tool" data-action="sessions" title="历史会话" aria-label="历史会话">${c.icon('history', 18)}</button>
          <button class="rail-tool" data-action="workers" title="Worker 装配" aria-label="Worker 装配">${c.icon('layers', 18)}</button>
          <button class="rail-tool" data-action="artifact" title="工作产物" aria-label="工作产物">${c.icon('file', 18)}</button>
          <div class="rail-tools-spacer"></div>
          <button class="rail-tool" data-action="help" title="帮助与快捷键" aria-label="帮助与快捷键">${c.icon('help', 18)}</button>
        </nav>
        <section class="rail-main" aria-label="构建对话">
          <header class="rail-main-head">
            <div class="rail-breadcrumb">${c.icon('workflow', 13)} <span>workspace</span><span class="rail-breadcrumb-slash">/</span><strong>${c.e(s.workflowName || 'new-session')}</strong></div>
            <button class="plain-button rail-mode-control" data-action="mode">${c.icon(s.mode === 'plan' ? 'brain' : 'bolt', 13)} ${c.e(s.mode.toUpperCase())}</button>
          </header>
          <div class="rail-chat-scroll">
            <div class="rail-chat-column">
              <div class="rail-context-line"><span class="rail-context-marker"></span> SESSION <span class="rail-context-model">${c.e(s.model)}</span></div>
              ${fresh ? `<section class="rail-welcome">
                <div class="rail-welcome-index">01 / START SOMETHING</div>
                <h1>一个想法。<br><span>多条路径，一起推进。</span></h1>
                <p>把任务交给团队，在这里推敲方向、追问结果。<br>每个节点的思考与产物，都可以沿右侧路线深入。</p>
                <div class="rail-start-options">
                  <button data-template="research"><span>${c.icon('search', 16)} 探索与研究</span><span>01 ${c.icon('arrow', 14)}</span></button>
                  <button data-template="review"><span>${c.icon('git', 16)} 方案交叉审阅</span><span>02 ${c.icon('arrow', 14)}</span></button>
                  <button data-template="launch"><span>${c.icon('bolt', 16)} 从构想到交付</span><span>03 ${c.icon('arrow', 14)}</span></button>
                </div>
              </section>` : `<header class="rail-task"><h1>${c.e(s.task || s.workflowName || '新的构建')}</h1><p>对话决定方向，节点并行推进。</p></header>`}
              <div class="rail-messages">${c.chat(s, { welcome: false })}</div>
              ${s.status === 'review' || s.status === 'failed' ? `<div class="rail-attention"><span>${c.icon(s.status === 'failed' ? 'terminal' : 'eye', 17)}</span><div><strong>${s.status === 'failed' ? '执行需要你的关注' : '到达人工检查点'}</strong><p>${s.status === 'failed' ? '选择右侧失败节点，查看活动记录并重试。' : '查看当前节点结果，确认后继续构建。'}</p></div><button class="plain-button" data-action="inspector">打开节点 ${c.icon('arrow', 14)}</button></div>` : ''}
              ${s.status === 'completed' ? `<button class="rail-delivery" data-action="artifact"><span>${c.icon('check', 18)} 构建已完成，查看最终产物</span>${c.icon('arrow', 17)}</button>` : ''}
            </div>
          </div>
          <footer class="rail-composer-dock">
            <div class="rail-input-label"><span><span class="rail-prompt-symbol">›</span> YOUR DIRECTION</span><span>${c.e(s.mode === 'plan' ? '先推敲，再出发' : '让想法开始运行')}</span></div>
            ${c.composer(s, { compact: true, placeholder: '描述下一步，或输入 / 调度工作流…' })}
            <div class="rail-input-foot"><span>${c.icon('command', 11)} 命令与自然语言，随时切换</span><button class="plain-button" data-action="workers">${c.icon('layers', 12)} 装配 Worker</button></div>
          </footer>
        </section>
        <aside class="rail-execution" aria-label="执行路线与节点详情">
          <header class="rail-execution-head"><span class="eyebrow">EXECUTION RAIL</span><span class="rail-live-dot" data-status="${c.e(s.status)}"></span></header>
          <section class="rail-run-summary">
            <div class="rail-run-status"><strong>${c.e(stateNames[s.status])}</strong><span>${completeCount}<span class="rail-count-slash">/</span>${s.nodes.length}</span></div>
            <div class="rail-progress">${c.progress(s)}</div>
            <div class="rail-run-meta"><span>${runningCount ? `${runningCount} 个节点同时运行` : s.status === 'idle' ? '等待启动流程' : '直接上游结果自动汇入'}</span>${c.metrics(s)}</div>
            <div class="rail-run-controls">${c.runControls(s)}</div>
          </section>
          <section class="rail-route">
            <div class="rail-section-head"><span>ROUTE MAP</span><button class="plain-button" data-action="inspector" title="展开所选节点">${c.icon('expand', 13)} 展开</button></div>
            <div class="rail-graph-scroll">${c.graph(s, { orientation: 'vertical', compact: true })}</div>
          </section>
          <section class="rail-node-detail">
            <div class="rail-section-head"><span>NODE ${c.e(selected ? selected.id : '—')}</span><span class="rail-selected-worker">${c.e(selected ? selected.label : '选择路线节点')}</span></div>
            <div class="rail-inspector-scroll">${c.inspector(s, { compact: true })}</div>
          </section>
          <button class="rail-artifact-link" data-action="artifact"><span>${c.icon('code', 15)} 工作产物</span><span>PREVIEW ${c.icon('arrow', 13)}</span></button>
        </aside>
      </div>`;
    }
  };
}());
