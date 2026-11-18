(function () {
  'use strict';

  window.CTflowVariants.canvas = {
    name: '编排',
    subtitle: '把想法展开为可执行的工作流，在画布上检查每一次交接。',
    label: 'CANVAS',
    render(s, c) {
      const selected = s.nodes.find((node) => node.id === s.selectedNode);
      const done = s.nodes.filter((node) => node.status === 'completed').length;
      const running = s.nodes.filter((node) => node.status === 'running');
      const states = {
        idle: '等待指令', running: '正在编排执行', paused: '执行已暂停',
        review: '等待人工检查', failed: '节点需要处理', completed: '工作流已完成',
        stopped: '执行已停止',
      };
      const nodeStates = {
        pending: '待执行', running: '运行中', completed: '已完成',
        failed: '执行失败', blocked: '等待上游',
      };

      return `<div class="variant-canvas">
        <div class="canvas-body">
          <main class="canvas-workspace" aria-label="工作流编排画布">
            <header class="canvas-toolbar">
              <div class="canvas-title">
                <span class="eyebrow">WORKFLOW / CANVAS</span>
                <h1>${c.e(s.workflowName || '新的工作流')}</h1>
              </div>
              <div class="canvas-tools">
                <button class="plain-button" data-action="workers">${c.icon('layers', 15)}<span>Worker</span></button>
                <button class="icon-button" data-action="sessions" title="工作流历史" aria-label="工作流历史">${c.icon('history', 16)}</button>
              </div>
            </header>
            <div class="canvas-worker-rail">
              <span class="canvas-rail-label">装配资源</span>
              <div class="canvas-worker-items">${c.workers(s, { compact: true })}</div>
            </div>
            <section class="canvas-map-section" aria-label="节点及依赖关系">
              <div class="canvas-map-caption">
                <span>${c.icon('workflow', 14)} 直接依赖 · 独立上下文</span>
                <span>${s.nodes.length} NODES</span>
              </div>
              <div class="canvas-map">${c.graph(s, { orientation: 'horizontal', compact: false })}</div>
              <div class="canvas-map-footer">
                <span>选择节点，查看交接的结果与依据</span>
                <button class="plain-button" data-action="inspector">${c.icon('expand', 14)} 深入当前节点</button>
              </div>
            </section>
            <div class="canvas-execution">
              <div class="canvas-execution-top">
                <span class="canvas-run-label ${s.status === 'running' ? 'is-active' : ''}"><i></i>${states[s.status]}</span>
                <span class="canvas-completion">${done}<span> / ${s.nodes.length} 完成</span></span>
              </div>
              ${c.progress(s)}
              <div class="canvas-execution-bottom">
                <span class="canvas-current">${running.length ? `当前：${running.map((node) => c.e(node.name)).join(' · ')}` : c.e(s.task || '描述目标，让工作流从这里开始。')}</span>
                <div class="canvas-controls">${c.runControls(s)}</div>
              </div>
            </div>
          </main>
          <aside class="canvas-inspect" aria-label="节点检查器">
            <header class="canvas-inspect-heading">
              <span class="eyebrow">NODE / INSPECT</span>
              <button class="icon-button" data-action="inspector" aria-label="展开节点详情" title="展开节点详情">${c.icon('expand', 15)}</button>
            </header>
            <div class="canvas-node-heading">
              <span class="canvas-node-id">${c.e(selected ? selected.id : '—')}</span>
              <div><h2>${c.e(selected ? selected.name : '选择一个节点')}</h2><span class="canvas-node-state">${selected ? nodeStates[selected.status] : '在画布中查看执行路径'}</span></div>
            </div>
            <div class="canvas-inspector-content">${c.inspector(s, { compact: true })}</div>
            <button class="canvas-artifact-link" data-action="artifact">${c.icon('file', 16)}<span>打开工作流产物</span>${c.icon('arrow', 15)}</button>
          </aside>
        </div>
        <footer class="canvas-dock">
          <div class="canvas-feedback">
            <span class="canvas-feedback-label">${c.icon('terminal', 14)} 对话回执</span>
            <div class="canvas-chat">${c.chat(s, { compact: true, welcome: false })}</div>
          </div>
          <div class="canvas-command-row">
            <div class="canvas-command-input">${c.composer(s, { placeholder: '描述目标，或输入 / 编排工作流…', compact: true })}</div>
            <button class="canvas-starter plain-button" data-template="research">${c.icon('plus', 15)} 研究方案</button>
          </div>
        </footer>
      </div>`;
    },
  };
})();
