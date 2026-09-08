/* 节点池管理 —— edit dispatch parameters without a shell.

   Pulling a node out of rotation is an incident action: it happens when a
   node is misbehaving and it has to take effect now. So every field here
   saves through an endpoint that reconfigures the running scheduler in place,
   and the card shows configuration and live probe state side by side — the
   operator needs to see what they set *and* what the fleet is actually
   doing, or they cannot tell whether the change did anything. */

function poolStateTag(n) {
  if (!n.enabled) return '<span class="tag idle">已停用</span>';
  if (n.manually_disabled) return '<span class="tag warn">临时摘除</span>';
  if (!n.ok) return '<span class="tag bad">探针失败</span>';
  return '<span class="tag ok">在线</span>';
}

function poolProbeAge(n) {
  const ts = Number(n.last_success_ts || n.last_probe_ts || 0);
  if (!ts) return '<span class="muted">从未成功</span>';
  const age = (Date.now() / 1000) - ts;
  const cls = age > 120 ? 'danger-text' : '';
  return `<span class="${cls}">${esc(fmtAge(age))}前</span>`;
}

function nodePoolCard(n) {
  const util = Math.round(Number(n.utilisation || 0) * 100);
  const share = Math.round(Number(n.share || 0) * 100);
  const id = esc(n.name);
  return `
  <div class="card" data-live-key="pool:${id}">
    <div class="card-head">
      <div><h3>${esc(n.name)} ${poolStateTag(n)}</h3>
        <div class="sub">${esc(n.base_url || '')}</div></div>
      <div class="toolbar">
        <span class="muted">占比 ${share}%</span>
      </div>
    </div>
    <div class="card-body">
      <div class="stat-grid">
        ${stat('▶', n.active_streams || 0, '活跃流', '探针实时值')}
        ${stat('⇅', egressCell(n), '出口带宽', `占用率 ${util}% · 包含非播放流量`)}
        ${stat('◔', poolProbeAge(n), '探针', n.ok ? '最近一次成功' : '当前失败')}
      </div>
      <div class="form-row"><label>参与调度</label>
        <input type="checkbox" class="np-enabled" data-node="${id}"
          ${n.enabled ? 'checked' : ''}>
        <span class="muted">取消勾选后调度不再选中该节点</span></div>
      <div class="form-row"><label>权重</label>
        <input type="number" class="np-weight" data-node="${id}" min="1" max="100000"
          value="${esc(n.capacity)}" style="width:110px">
        <span class="muted">相对份额，同时作为并发容量上限</span></div>
      <div class="form-row"><label>带宽上限</label>
        <input type="number" class="np-bandwidth" data-node="${id}" min="0" max="1000000"
          value="${esc(n.bandwidth_mbps)}" style="width:110px">
        <span class="muted">Mbps，0 表示未知（只按路数判断负载）</span></div>
      <div class="toolbar">
        <button class="btn primary np-save" data-node="${id}">保存</button>
        <span class="np-result muted" data-node="${id}"></span>
      </div>
    </div>
  </div>`;
}

PAGES.nodepool = async (context = pageContext('nodepool')) => {
  renderView(pageLoading(), context);
  const [nodes, dispatch] = await Promise.all([
    api('/api/nodes/pool'), api('/api/settings/dispatch'),
  ]);
  if (!context.isCurrent()) return;
  PAGE_MODELS.nodepool = {nodes, dispatch};
  paintNodePool(PAGE_MODELS.nodepool, context);
};

function paintNodePool(model, context) {
  if (!context.isCurrent()) return;
  const {nodes, dispatch} = model;
  const enabled = nodes.filter((n) => n.enabled);
  const online = nodes.filter((n) => n.available).length;
  const streams = nodes.reduce((a, n) => a + (n.active_streams || 0), 0);
  const egress = egressSummary(nodes);

  renderView(`
    <div class="stat-grid">
      ${stat('⛁', `${online} / ${nodes.length}`, '在线节点',
    `${enabled.length} 个参与调度`)}
      ${stat('▶', streams, '活跃流', '所有节点合计')}
      ${stat('⇅', egress.text, '出口带宽', egress.sub)}
      ${stat('⚖', dispatch.policy === 'affinity' ? '文件亲和' : '最低负载', '调度策略',
    `阈值 ${Math.round(Number(dispatch.load_threshold || 0) * 100)}%`)}
    </div>
    ${enabled.length ? '' : card('⚠ 没有节点参与调度', '所有节点都已停用',
    `<div class="card-body"><div class="muted">当前不会有任何播放被分流到节点，全部回退由 Emby 直供。</div></div>`)}
    ${nodes.length ? nodes.map(nodePoolCard).join('')
    : `<div class="card"><div class="empty">尚未配置任何节点</div></div>`}`, context);

  document.querySelectorAll('.np-save').forEach((btn) => {
    btn.onclick = () => saveNodePool(btn.dataset.node);
  });
}
registerLiveUpdater('nodepool', ['nodes'], (topic, payload, context) => {
  const model = PAGE_MODELS.nodepool;
  if (!model) return;
  if (topic === 'nodes') {
    const total = payload.filter(n=>n.enabled).reduce((a,n)=>a+Number(n.capacity || 0),0);
    model.nodes = payload.map(n=>({...n,share:n.enabled && total ? Number(n.capacity || 0)/total : 0}));
  }
  if (topic === 'nodes') paintNodePool(model, context);
});

async function saveNodePool(name) {
  const pick = (cls) => document.querySelector(`.${cls}[data-node="${CSS.escape(name)}"]`);
  const result = pick('np-result');
  const btn = pick('np-save');
  const payload = {
    enabled: pick('np-enabled').checked,
    weight: Number(pick('np-weight').value),
    bandwidth_mbps: Number(pick('np-bandwidth').value),
  };
  btn.disabled = true;
  if (result) { result.textContent = '保存中…'; result.className = 'np-result muted'; }
  try {
    const saved = await api(`/api/nodes/${encodeURIComponent(name)}/pool`, {
      method: 'PUT', body: JSON.stringify(payload),
    });
    const changed = Object.keys(saved.changed || {});
    toast(changed.length ? `已保存并生效：${changed.join(', ')}` : '没有变化');
    await renderPage('nodepool');
  } catch (e) {
    toast('保存失败: ' + e.message, 1);
    if (result) {
      result.textContent = e.message;
      result.className = 'np-result danger-text';
    }
    btn.disabled = false;
  }
}
