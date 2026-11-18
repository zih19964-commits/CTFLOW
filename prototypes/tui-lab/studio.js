(function () {
  'use strict';

  const statusLabels = {
    idle: '等待开题', running: '正在成稿', paused: '创作已暂停',
    review: '等待你审阅', failed: '需要处理', completed: '成稿就绪', stopped: '已停止'
  };

  function emptyDocument(c) {
    return `<div class="studio-empty">
      <div class="studio-folio">CT / STUDIO <span>001</span></div>
      <p class="studio-kicker">想法 → 依据 → 成稿</p>
      <h2>把一个想法，<br>写成一份值得交付的作品。</h2>
      <p class="studio-empty-description">在左侧描述目标。研究、推敲与审阅沿着上方步骤展开，成果会在这里逐步成形。</p>
      <div class="studio-starters" aria-label="选择创作起点">
        <button data-template="review"><span>01</span><strong>审阅一份方案</strong>${c.icon('arrow', 15)}</button>
        <button data-template="research"><span>02</span><strong>研究一个问题</strong>${c.icon('arrow', 15)}</button>
        <button data-template="launch"><span>03</span><strong>策划一次发布</strong>${c.icon('arrow', 15)}</button>
      </div>
      <div class="studio-empty-footer"><span>每一份结论，都有来处。</span><span>READY TO MAKE</span></div>
    </div>`;
  }

  function workingDocument(s, c) {
    const done = s.nodes.filter(node => node.status === 'completed').length;
    return `<div class="studio-working" aria-label="成稿编排中">
      <div class="studio-folio">LIVE MANUSCRIPT <span>${String(done).padStart(2, '0')} / ${String(s.nodes.length).padStart(2, '0')}</span></div>
      <p class="studio-kicker">${c.e(statusLabels[s.status])}</p>
      <h2>${c.e(s.workflowName)}</h2>
      <p class="studio-working-description">${s.status === 'running' ? '各步骤正在汇集依据。完成的节点可随时打开，查看结果与上下文。' : '当前进度已保留。通过上方运行控制继续创作，或打开节点检查已有结果。'}</p>
      <div class="studio-manuscript-lines" aria-hidden="true">
        <i></i><i></i><i></i><b></b><i></i><i></i><i></i><b></b><i></i><i></i>
      </div>
      <button class="secondary-button" data-action="inspector">${c.icon('eye', 15)} 查看当前节点</button>
    </div>`;
  }

  window.CTflowVariants.studio = {
    name: '成稿',
    subtitle: '把对话与依据，编排成值得交付的作品',
    label: 'STUDIO',
    render(s, c) {
      const selected = s.nodes.find(node => node.id === s.selectedNode);
      const complete = s.nodes.filter(node => node.status === 'completed').length;
      const ready = s.status === 'completed' || s.status === 'review';
      const hasOutput = s.nodes.some(node => node.output);
      const document = s.status === 'idle' ? emptyDocument(c)
        : ready || hasOutput ? c.artifact(s, { compact: false })
          : workingDocument(s, c);

      return `<div class="variant-studio studio-status-${c.e(s.status)}">
        <header class="studio-masthead">
          <div class="studio-title-group"><span class="studio-mark">${c.icon('file', 19)}</span><div><h1>成稿 <span>/ STUDIO</span></h1><p>${c.e(s.workflowName)} <span>·</span> 终端创作台</p></div></div>
          <div class="studio-run-controls">${c.runControls(s)}</div>
        </header>

        <section class="studio-route" aria-label="创作流程与阶段">
          <div class="studio-route-heading"><span class="studio-kicker">创作路径</span><span class="studio-stage-count">${String(complete).padStart(2, '0')} / ${String(s.nodes.length).padStart(2, '0')} 步骤</span><div class="studio-progress">${c.progress(s)}</div><span class="studio-status-label">${c.e(statusLabels[s.status])}</span></div>
          <div class="studio-graph">${c.graph(s, { orientation: 'horizontal', compact: true })}</div>
        </section>

        <div class="studio-workspace">
          <section class="studio-conversation" aria-label="创作对话与命令输入">
            <header class="studio-pane-heading"><span>${c.icon('terminal', 14)} 创作对话</span><button class="plain-button" data-action="sessions">${c.icon('history', 14)} 历史</button></header>
            <div class="studio-chat">${c.chat(s, { compact: false, welcome: true })}</div>
            <div class="studio-prompt-suggestions"><button data-insert="/plan ">${c.icon('brain', 13)} 先推敲方向</button><button data-action="workers">${c.icon('layers', 13)} 创作团队</button><button data-action="help">${c.icon('command', 13)} 命令</button></div>
            <div class="studio-composer">${c.composer(s, { placeholder: '描述你想完成的作品，或输入 / 选择命令…', compact: false })}</div>
            <div class="studio-session-meta"><span>对话推动创作 · 节点保留依据</span><div>${c.metrics(s)}</div></div>
          </section>

          <section class="studio-desk" aria-label="作品与证据">
            <header class="studio-pane-heading"><span>${c.icon('file', 14)} 作品</span><span class="studio-document-state"><i></i>${ready ? '可阅读成稿' : s.status === 'idle' ? '等待第一笔' : '工作稿'}</span></header>
            <div class="studio-document" data-studio-document="${ready ? 'ready' : 'draft'}">${document}</div>
            <section class="studio-evidence" aria-label="所选节点的证据与上下文">
              <header class="studio-evidence-heading"><div><span class="studio-kicker">依据 / TRACE</span><span class="studio-evidence-node">${selected ? `${c.e(selected.id)} · ${c.e(selected.name)}` : '选择上方步骤，查看依据'}</span></div><button class="icon-button" data-action="inspector" title="展开节点详情" aria-label="展开节点详情">${c.icon('expand', 14)}</button></header>
              <div class="studio-inspector">${c.inspector(s, { compact: true })}</div>
            </section>
          </section>
        </div>
      </div>`;
    }
  };
}());
