/* 入库流水线 —— one screen for "why has nothing arrived?"

   The page an operator opens mid-incident. Three rules follow from that:

   - The verdict comes first. A red/amber/green banner with the reasons
     spelled out, above everything else, so the answer is readable without
     interpreting six cards.
   - A section that could not be read says so. "Unknown" and "zero" lead to
     opposite conclusions, and a card that renders 0 for a missing directory
     is worse than one that admits it could not look.
   - One broken section never blanks the page. Every card is built from its
     own slice of the snapshot and degrades on its own. */

const INTAKE_LEVEL = {
  ok: ['ok', '正常'],
  warn: ['warn', '注意'],
  bad: ['bad', '异常'],
  idle: ['idle', '已抑制'],
};

function intakeNA(reason) {
  return `<div class="empty">${esc(reason || '暂不可用')}</div>`;
}

/* A section is renderable only when it exists and reported success. Callers
   pass what to draw; this keeps the "unavailable" branch in one place instead
   of repeating the same ternary in every card. */
function intakeSection(section, draw) {
  if (!section) return intakeNA('未采集');
  if (section.available === false) return intakeNA(section.reason);
  return draw(section);
}

function intakeBanner(health, ageSeconds, stale) {
  const [cls, label] = INTAKE_LEVEL[(health && health.level) || 'ok'] || INTAKE_LEVEL.ok;
  const alerts = (health && health.alerts) || [];
  const age = ageSeconds == null ? '—' : `${Math.round(ageSeconds)} 秒前采集`;
  return `
    <div class="card" data-live-key="intake-health">
      <div class="card-head">
        <div>
          <h3>流水线健康 <span class="tag ${cls}">${esc(label)}</span></h3>
          <div class="sub">${esc(age)}${stale ? ' · 快照已过期' : ''}</div>
        </div>
        <div class="toolbar"><button class="btn sm" id="intake-refresh">立即采集</button></div>
      </div>
      ${alerts.length
        ? `<div class="card-body flush">${alerts.map((a) => {
          const [acls] = INTAKE_LEVEL[a.level] || INTAKE_LEVEL.idle;
          return `<div class="list-row"><div class="t">${esc(a.message)}</div>
            <span class="tag ${acls}">${esc((INTAKE_LEVEL[a.level] || INTAKE_LEVEL.idle)[1])}</span></div>`;
        }).join('')}</div>`
        : '<div class="card-body"><span class="muted">没有触发任何告警规则。</span></div>'}
    </div>`;
}

function intakeScanCard(emby) {
  const scan = emby && emby.scan;
  const latest = emby && emby.latest;
  const probe = emby && emby.probe;

  const scanBody = intakeSection(scan, (s) => {
    const pct = s.progress == null ? null : Math.max(0, Math.min(100, s.progress));
    return `<div class="card-body">
      <div><b>${esc(s.name || '媒体库扫描')}</b>
        <span class="tag ${s.running ? 'warn' : 'idle'}">${esc(s.running ? '扫描中' : (s.state || '空闲'))}</span></div>
      ${pct == null ? '' : `<div class="bar wide" style="margin-top:10px"><i style="width:${pct}%"></i></div>
        <div class="s muted" style="margin-top:4px">进度 ${pct.toFixed(1)}%</div>`}
      <div class="s muted" style="margin-top:8px">上次结果：${esc(s.last_status || '未知')}
        ${s.last_end_age_seconds == null ? '' : ` · ${esc(fmtAge(s.last_end_age_seconds))}前结束`}</div>
    </div>`;
  });

  const latestBody = intakeSection(latest, (l) => `<div class="card-body">
      <div class="val" style="font-size:22px;font-weight:600">${esc(fmtAge(l.age_seconds))}前</div>
      <div class="s muted" style="margin-top:4px">最新入库条目类型：${esc(l.type || '-')}</div>
    </div>`);

  const probeBody = intakeSection(probe, (p) => {
    const rows = (p.groups || []).map((g) => {
      const pct = Math.round((g.ratio || 0) * 100);
      return `<tr><td>${esc(g.path)}</td><td>${esc(g.count)}</td>
        <td>${pct}% <span class="bar ${pct > 50 ? 'bad' : ''}"><i style="width:${pct}%"></i></span></td></tr>`;
    }).join('');
    return `<div class="card-body flush">
      <table><thead><tr><th>目录</th><th>次数</th><th>占比</th></tr></thead>
      <tbody>${rows}</tbody></table>
      <div class="s muted" style="padding:10px 18px">样本 ${esc(p.samples)} 条探测记录；单目录占比过高通常意味着探测循环。</div>
    </div>`;
  });

  return `
    ${card('媒体库扫描', '扫描任务状态与进度', scanBody)}
    ${card('最新入库', '距离上一次成功入库的时间', latestBody)}
    ${card('探测热点', '最近探测记录按目录聚合', probeBody)}`;
}

function intakeRefreshCard(refresh) {
  return card('刷新队列', '等待通知媒体服务器的目录', intakeSection(refresh, (r) => {
    const rows = (r.top || []).map((t) => `<tr>
      <td>${esc(t.label)}</td><td>${esc(t.events)}</td><td>${esc(t.paths)}</td>
      <td>${esc(fmtAge(t.age_seconds))}</td></tr>`).join('');
    return `<div class="card-body">
        <div class="stat-grid">
          ${stat('⇄', r.total, '队列条目', r.truncated ? '已达扫描上限' : '待推送目录')}
          ${stat('⏱', fmtAge(r.oldest_age_seconds), '最老条目', '等待时长')}
          ${stat(r.suppressed ? '⏸' : '▶', r.suppressed ? '已抑制' : '正常',
    '推送开关', r.suppressed ? '抑制开关文件存在' : '未设置抑制')}
        </div>
        ${r.unreadable ? `<div class="s muted">${esc(r.unreadable)} 个条目无法解析（已跳过）</div>` : ''}
      </div>
      ${rows ? `<div class="card-body flush"><table>
        <thead><tr><th>目录</th><th>事件</th><th>文件</th><th>等待</th></tr></thead>
        <tbody>${rows}</tbody></table></div>` : '<div class="empty">队列为空</div>'}`;
  }));
}

function intakeNotifyCard(notify) {
  const pending = notify && notify.pending;
  const last = notify && notify.last_sent;
  const pendingBody = intakeSection(pending, (p) => `<div class="card-body">
      <div class="stat-grid">
        ${stat('✉', p.total, '待发通知', '等待入库确认')}
        ${stat('⏱', fmtAge(p.oldest_age_seconds), '最老一条', '入队时长')}
      </div></div>`);
  const lastBody = intakeSection(last, (l) => `<div class="card-body">
      <div class="s">${esc(l.line)}</div>
      <div class="s muted" style="margin-top:6px">${
  l.age_seconds == null ? '时间未知' : `${esc(fmtAge(l.age_seconds))}前发出`}</div>
    </div>`);
  return `${card('通知积压', '已入库但尚未通知的剧集', pendingBody)}
    ${card('最近发出的通知', '日志中最后一条成功记录', lastBody)}`;
}

function intakeUploadCard(upload) {
  if (!upload) return card('上传通道', '', intakeNA('未采集'));
  const lanes = upload.lanes;
  const lanesBody = intakeSection(lanes, (l) => {
    const rows = (l.lanes || []).map((x) => `<tr><td>${esc(x.name)}</td>
      <td>${esc(x.items)}</td><td>${fmtBytes(x.bytes)}</td></tr>`).join('');
    return `<div class="card-body">
        <div class="stat-grid">
          ${stat('⇪', l.items, '待上传文件', '所有通道合计')}
          ${stat('⛁', fmtBytes(l.bytes), '待上传体积', '所有通道合计')}
        </div></div>
      ${rows ? `<div class="card-body flush"><table>
        <thead><tr><th>通道</th><th>文件</th><th>体积</th></tr></thead>
        <tbody>${rows}</tbody></table></div>` : '<div class="empty">通道均为空</div>'}`;
  });

  const buffers = (upload.buffers || []).map((b) => {
    const label = ({ staging: '整理暂存', 'local-fallback': '本机应急', quarantine: '隔离区' })[b.name] || b.name;
    return b.available
      ? stat('⛃', fmtBytes(b.bytes), label, `${b.items} 个文件`)
      : stat('⛃', '—', label, '目录不可读');
  }).join('');

  const limited = upload.rate_limited || [];
  const limitBody = upload.rate_limited_known === false
    ? intakeNA('限速标记目录不可读')
    : (limited.length
      ? `<div class="card-body flush">${limited.map((n) =>
        `<div class="list-row"><div class="t">${esc(n)}</div>
          <span class="tag warn">限速中</span></div>`).join('')}</div>`
      : '<div class="card-body"><span class="muted">没有身份处于限速状态。</span></div>');

  return `${card('上传通道', '等待推送到云端的文件', lanesBody)}
    ${card('本地缓冲区', '暂存、应急与隔离目录占用',
    buffers ? `<div class="card-body"><div class="stat-grid">${buffers}</div></div>`
      : intakeNA('未配置'))}
    ${card('上传限速', '云端配额限制标记', limitBody)}`;
}

function intakeCloudCard(cloud) {
  if (!cloud) return card('云端拉取', '', intakeNA('未采集'));
  const claims = cloud.claims;
  /* outstanding is null when the listing was capped: a partial listing cannot
     support claims-minus-receipts, and a fabricated backlog is worse than an
     admitted unknown. */
  const claimsBody = intakeSection(claims, (c) => `<div class="card-body">
      <div class="stat-grid">
        ${stat('⇩', c.outstanding == null ? '—' : c.outstanding, '未完成任务',
    c.outstanding == null ? '条目过多，无法精确统计' : `共 ${c.total} 个认领`)}
        ${stat('✓', c.done, '已完成', c.truncated ? '已达扫描上限' : '有完成回执')}
      </div></div>`);

  const backlog = cloud.backlog;
  const queue = cloud.queue;
  const active = cloud.active;
  const rows = [
    ['积压条目', backlog && backlog.available ? backlog.rows : null,
      backlog && backlog.available ? null : (backlog && backlog.reason)],
    ['队列深度', queue && queue.available ? queue.depth : null,
      queue && queue.available ? null : (queue && queue.reason)],
    ['当前任务清单', active && active.available ? active.manifest_items : null,
      active && active.available ? null : (active && active.reason)],
    ['待识别', cloud.pending_identity, cloud.pending_identity == null ? '目录不可读' : null],
    ['事件文件', cloud.events, cloud.events == null ? '目录不可读' : null],
  ].map(([label, value, reason]) => `<div class="list-row">
      <div class="t">${esc(label)}</div>
      <div>${value == null ? `<span class="muted">${esc(reason || '未知')}</span>` : `<b>${esc(value)}</b>`}</div>
    </div>`).join('');

  return `${card('云端拉取', '认领与完成回执', claimsBody)}
    ${card('拉取队列', '积压、队列与当前任务', `<div class="card-body flush">${rows}</div>`)}`;
}

function intakeDownloaderCard(downloader) {
  const clients = (downloader && downloader.clients) || [];
  if (!clients.length) {
    return card('下载器', '完成但尚未消化的任务', intakeNA('未配置下载器'));
  }
  const rows = clients.map((c) => {
    if (!c.available) {
      return `<tr><td>${esc(c.name)}</td><td colspan="4">
        <span class="muted">不可用：${esc(c.reason || '未知')}</span></td></tr>`;
    }
    return `<tr><td>${esc(c.name)}</td>
      <td>${esc(c.total)}</td>
      <td>${esc(c.downloading)} <span class="muted">${fmtBytes(c.downloading_bytes)}</span></td>
      <td>${esc(c.completed)} <span class="muted">${fmtBytes(c.completed_bytes)}</span></td></tr>`;
  }).join('');
  return tableCard('下载器', '已完成但尚未进入下一步的任务最值得关注',
    ['下载器', '任务总数', '下载中', '已完成'], rows);
}

PAGES.intake = async (context = pageContext('intake')) => {
  renderView(pageLoading(), context);
  const snap = await api('/api/intake').catch((e) => ({available:false, reason:e.message}));
  paintIntake(snap, context);
};
function paintIntake(snap, context) {
  if (!context.isCurrent()) return;
  if (!snap || snap.available === false) {
    renderView(`<div class="card"><div class="card-head">
        <div><h3>入库流水线</h3><div class="sub">${esc(snap && snap.reason ? snap.reason : '快照不可用')}</div></div>
        <div class="toolbar"><button class="btn sm" id="intake-refresh">立即采集</button></div>
      </div><div class="empty">${esc(snap && snap.reason ? snap.reason : '尚未采集')}</div></div>`, context);
    bindIntakeRefresh();
    return;
  }

  const d = snap.data || {};
  renderView(`
    ${intakeBanner(d.health, snap.snapshot_age_seconds, snap.stale)}
    ${snap.error ? card('采集告警', '最近一次采集失败，下方数据为上一次成功结果',
    `<div class="card-body"><span class="danger-text">${esc(snap.error)}</span></div>`) : ''}
    ${intakeScanCard(d.emby)}
    ${intakeRefreshCard(d.refresh)}
    ${intakeNotifyCard(d.notify)}
    ${intakeUploadCard(d.upload)}
    ${intakeCloudCard(d.cloud)}
    ${intakeDownloaderCard(d.downloader)}`, context);
  bindIntakeRefresh();
}
registerLiveUpdater('intake', ['intake'], (topic, payload, context) => {
  if (topic === 'intake') paintIntake(payload, context);
});

function bindIntakeRefresh() {
  const btn = $('#intake-refresh');
  if (!btn) return;
  btn.onclick = async () => {
    if (btn.disabled) return;
    const context = pageContext('intake');
    btn.disabled = true;
    btn.textContent = '采集中…';
    try {
      await api('/api/intake/refresh', { method: 'POST' });
      toast('已重新采集');
      /* Clear the pushed copy first: rendering would otherwise show the stale
         snapshot the stream last delivered and look like the button did
         nothing. */
      if (!context.isCurrent()) return;
      if (live.data) delete live.data.intake;
      await renderPage('intake');
    } catch (e) {
      toast('采集失败: ' + e.message, 1);
      btn.disabled = false;
      btn.textContent = '立即采集';
    }
  };
}
