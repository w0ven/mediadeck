/* mediadeck panel shell.
   Sidebar-grouped navigation + dashboard/stat-card layout. Pages are rendered
   into #view; each page declares its own loader so new modules only need a
   PAGES entry. */

const state = { page: 'dashboard', timer: null };
const $ = (s) => document.querySelector(s);

/* Grouped by what the operator is doing, not by which subsystem implements it.
   Approvals and invites used to sit under "Telegram" because the bot delivers
   them, and requests under "operations" because they are a business feature --
   which meant finding either one required knowing the implementation first.
   Everything about *who may watch* is now in one place, everything about
   *what there is to watch* in another. */
const NAV = [
  { group: '总览', icon: '▦', items: [
    { id: 'dashboard', icon: '▦', label: '仪表盘', sub: '集中查看系统运行、播放使用和待处理事项' },
    { id: 'stats', icon: '📈', label: '运营统计', sub: '流量、时长与热门内容' },
  ]},
  { group: '用户', icon: '☺', items: [
    { id: 'members', icon: '☺', label: '用户管理', sub: '账号、套餐、邀请关系与积分' },
    { id: 'groups', icon: '▣', label: '套餐与用户组', sub: '时长、流量、并发与求片次数' },
    { id: 'invites', icon: '🎫', label: '邀请与授权', sub: '预授权名单、邀请名额与邀请树' },
    { id: 'redeem', icon: '🎟', label: '卡密管理', sub: '生成、发放与作废注册卡密' },
  ]},
  { group: '内容', icon: '▤', items: [
    { id: 'library', icon: '▤', label: '媒体库', sub: '媒体库分布与条目统计' },
    { id: 'requests', icon: '🎬', label: '求片', sub: '成员求片、上片员接单与处理结果' },
    { id: 'imports', icon: '⇪', label: '网盘上片', sub: '网盘链接与云盘目录导入' },
    { id: 'intake', icon: '⇉', label: '入库流水线', sub: '一屏看完扫描、刷新、通知、上传与拉取' },
  ]},
  { group: '机器人', icon: '✈', items: [
    { id: 'tgbot', icon: '✈', label: '机器人', sub: '注册通道、名额与运行状态' },
    { id: 'shop', icon: '🎁', label: '兑换商城', sub: '积分商品、限购与兑换记录' },
    { id: 'tggroup', icon: '⚑', label: '群组核查', sub: '已关联成员的群成员状态' },
    { id: 'automation', icon: '⚡', label: '任务中心', sub: '任务与玩法插件的开关、配置与运行结果' },
  ]},
  { group: '运行维护', icon: '⛁', items: [
    { id: 'nodes', icon: '⛁', label: '节点管理', sub: '推流节点负载与调度' },
    { id: 'nodepool', icon: '⚖', label: '节点池', sub: '启停、权重、带宽与实时负载' },
    { id: 'pipeline', icon: '⇄', label: '管线状态', sub: '整理、上传队列与配额' },
    { id: 'storage', icon: '☁', label: '存储管理', sub: '云盘账号与挂载点' },
    { id: 'mounts', icon: '⛃', label: '挂载管理', sub: '存储挂载健康与缓存占用' },
    { id: 'tasks', icon: '⏱', label: '调度中心', sub: '主机定时任务运行状态与失败追踪' },
    { id: 'access', icon: '🛡', label: '访问拦截', sub: '客户端与网段规则，以及被拒记录' },
    { id: 'sharing', icon: '👥', label: '共享检测', sub: '同时多地播放的账号，只记录不处理' },
    { id: 'audit', icon: '☰', label: '审计日志', sub: '操作记录与变更追踪' },
  ]},
  { group: '设置', icon: '⚙', items: [
    { id: 'settings', icon: '⚙', label: '系统设置', sub: '对接 Emby、TMDB、调度策略与节点配置' },
    { id: 'update', icon: '⟳', label: '版本更新', sub: '检查并应用新版本' },
  ]},
];

/* Sentinel understood by the backend as "keep the stored secret". Lets the
   operator edit a URL without re-typing the API key. */
const SECRET_KEEP = '__KEEP__';

const PAGES = {};

/* ---------------- helpers ---------------- */
function toast(msg, bad) {
  const t = $('#toast');
  t.textContent = msg;
  t.style.background = bad ? '#e5484d' : '#1f2937';
  t.style.display = 'block';
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.style.display = 'none'), 3200);
}
function bindAsyncButton(id, action) {
  const button = document.getElementById(id);
  if (!button) return;
  button.onclick = async () => {
    if (button.disabled) return;
    button.disabled = true; button.setAttribute('aria-busy', 'true');
    try { await action(); }
    catch (error) { toast('操作失败: ' + error.message, 1); }
    finally { button.disabled = false; button.removeAttribute('aria-busy'); }
  };
}
async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  if (!r.ok) {
    let d = '';
    try { d = (await r.json()).detail || ''; } catch (e) { /* non-json error */ }
    throw new Error(`${r.status} ${d}`);
  }
  return r.json();
}
function fmtBytes(n) {
  if (!n) return '0';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + ' ' + u[i];
}
function fmtAge(s) {
  if (!s) return '-';
  if (s < 3600) return Math.round(s / 60) + ' 分钟';
  if (s < 86400) return (s / 3600).toFixed(1) + ' 小时';
  return (s / 86400).toFixed(1) + ' 天';
}
// HTML attribute escaping alone does not quote an embedded JavaScript string.
function jsArg(value) { return esc(JSON.stringify(String(value == null ? '' : value))); }
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
const stat = (icon, val, label, sub) => `
  <div class="stat" data-live-key="stat:${esc(label)}"><div class="ic-box">${icon}</div>
    <div class="val">${val}</div><div class="label">${esc(label)}</div>
    <div class="sub">${esc(sub || '')}</div></div>`;
const card = (title, sub, body, actions) => `
  <div class="card" data-live-key="card:${esc(title)}">
    <div class="card-head">
      <div><h3>${esc(title)}</h3>${sub ? `<div class="sub">${esc(sub)}</div>` : ''}</div>
      <div class="toolbar">${actions || ''}</div>
    </div>
    ${body}
  </div>`;
const tableCard = (title, sub, cols, rowsHtml, actions) => card(
  title, sub,
  `<div class="card-body flush">${rowsHtml
    ? `<table><thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${rowsHtml}</tbody></table>`
    : `<div class="empty">暂无数据</div>`}</div>`,
  actions);

function fmtAgeTs(ts) {
  if (!ts) return '-';
  return fmtAge((Date.now() / 1000) - Number(ts)) + '前';
}
function fmtMoney(cents, currency) {
  const n = Number(cents || 0) / 100;
  return (currency || 'CNY') + ' ' + n.toFixed(2);
}
function fmtQuota(n) {
  return n ? fmtBytes(n) : '不限';
}
/* ---------- artwork + playback ----------
   Posters are addressed through the panel's own cached-image route, never
   Emby directly: a dashboard renders a dozen tiles and auto-refreshes, and
   Emby re-derives every thumbnail it is asked for. */
function posterUrl(itemId, maxHeight, tag) {
  return `/emby/Items/${encodeURIComponent(itemId)}/Images/Primary`
    + `?maxHeight=${maxHeight || 420}&quality=88${tag ? '&tag=' + encodeURIComponent(tag) : ''}`;
}
function ticksToClock(ticks) {
  const total = Math.max(0, Math.floor(Number(ticks || 0) / 10000000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
const posterTile = (it) => `
  <figure class="poster">
    <div class="poster-img">
      <img src="${esc(posterUrl(it.Id, 420))}" alt="" loading="lazy">
      <span class="poster-badge">${it.Type === 'Series' ? '剧集' : '电影'}</span>
    </div>
    <figcaption class="poster-cap">
      <div class="t">${esc(it.Name || '')}</div>
      <div class="y">${esc(it.ProductionYear || '')}</div>
    </figcaption>
  </figure>`;
/* Compact row for the "who is watching" column. Progress is only drawn when
   the server actually reported it: a bar defaulting to 0% is indistinguishable
   from a session that genuinely just started. */
const playRow = (s) => {
  const pct = s.ProgressPercent;
  const known = pct !== null && pct !== undefined;
  return `
  <div class="play-row" data-live-key="session:${esc(s.Id)}">
    ${s.ItemId ? `<img class="thumb" src="${esc(posterUrl(s.ItemId, 180))}" alt="" loading="lazy">`
      : '<div class="thumb"></div>'}
    <div class="bd">
      <div class="t">${esc(s.SeriesName ? `${s.SeriesName} · ${s.Item}` : (s.Item || '-'))}</div>
      <div class="s">${s.Paused ? '' : '<span class="live-dot"></span>'}${esc(s.UserName || '-')} · ${esc(s.Client || '-')}</div>
      ${known ? `<div class="bar wide"><i style="width:${Math.min(100, pct)}%"></i></div>` : ''}
    </div>
    <div class="pct">${known ? `${esc(pct)}%` : '<span class="muted">—</span>'}</div>
  </div>`;
};
const playCard = (s) => {
  const pct = s.ProgressPercent;
  const known = pct !== null && pct !== undefined;
  const meta = [s.ItemType === 'Episode' ? '剧集' : '电影',
    ...(s.Genres || [])].filter(Boolean).join(' / ');
  return `
  <article class="play-card" data-live-key="session:${esc(s.Id)}">
    <div class="pc-poster" data-live-key="art:${esc(s.PosterItemId || s.ItemId || '')}:${esc(s.PosterImageTag || '')}">
      <div class="pc-art" data-live-preserve>
        <div class="pc-art-fallback"><span aria-hidden="true">▶</span><small>暂无海报</small></div>
        ${s.PosterItemId || s.ItemId ? `<img class="pc-art-image${Number(s.PosterAspectRatio) > 1 ? ' landscape' : ''}" src="${esc(posterUrl(s.PosterItemId || s.ItemId, 420, s.PosterImageTag))}" alt="${esc((s.SeriesName || s.Item || '影片') + '海报')}" width="120" height="180" loading="lazy" decoding="async">` : ''}
      </div>
      <span class="pc-live ${s.Paused ? 'paused' : ''}">${s.Paused ? '❚❚ 已暂停' : '● 播放中'}</span>
    </div>
    <div class="pc-bd">
      <div class="pc-title">${esc(s.SeriesName ? `${s.SeriesName} · ${s.Item}` : (s.Item || '-'))}${s.ProductionYear ? `（${esc(s.ProductionYear)}）` : ''}</div>
      <div class="pc-meta">${esc(meta || '—')}</div>
      ${s.Overview ? `<p class="pc-ov">${esc(s.Overview)}</p>` : ''}
      <div class="pc-user">
        <div class="avatar">${esc((s.UserName || '?').slice(0, 1).toUpperCase())}</div>
        <div><b>${esc(s.UserName || '-')}</b><span>${esc(s.Client || '-')}</span></div>
      </div>
      <div class="pc-speed">${sessionSpeedCell(s)}</div>
      ${known ? `<div class="bar wide"><i style="width:${Math.min(100, pct)}%"></i></div>
      <div class="pc-time"><span>${esc(ticksToClock(s.PositionTicks))}</span>
        <b>${esc(pct)}%</b><span>${esc(ticksToClock(s.RunTimeTicks))}</span></div>`
      : '<div class="pc-time"><span class="muted">进度不可用</span></div>'}
    </div>
  </article>`;
};

function sessionSpeedCell(s) {
  if (s.SpeedSource !== 'node') return '<span class="rate-unknown" title="没有有效节点实测；不使用媒体码率代替">未实测</span>';
  const count = Number(s.SpeedAccountSessions || 1);
  const scope = s.SpeedScope === 'user' ? `账号合计${count > 1 ? ` · ${count} 个会话共享，不可相加` : ''}` : '当前会话';
  return rateMarkup(s.SpeedBps, s.SpeedCollectedAt, s.SpeedTimeBasis, s.SpeedWindowSeconds, scope);
}
function pageError(err) {
  return `<div class="card"><div class="page-error">
    <div class="t">加载失败</div>
    <div>${esc(err && err.message ? err.message : err)}</div>
    <div style="margin-top:12px"><button class="btn" id="retry-page">重试</button></div>
  </div></div>`;
}
function pageLoading() {
  return '<div class="card"><div class="page-loading">加载中…</div></div>';
}
function trafficBar(used, quota) {
  if (!quota) return `<span class="muted">${fmtBytes(used)} / 不限</span>`;
  const pct = Math.max(0, Math.min(100, Math.round(used / quota * 100)));
  const cls = pct >= 100 ? 'bad' : pct >= 80 ? 'warn' : '';
  return `<div>${fmtBytes(used)} / ${fmtBytes(quota)}
    <span class="bar ${cls}"><i style="width:${pct}%"></i></span></div>`;
}
function stateTag(st) {
  const map = { active: ['ok', '正常'], suspended: ['warn', '已停用'], expired: ['bad', '已过期'],
    exhausted: ['bad', '已超额'], pending: ['warn', '待开通'] };
  const [cls, label] = map[st] || ['idle', st || '-'];
  return `<span class="tag ${cls}">${esc(label)}</span>`;
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast('已复制'); }
  catch (e) { toast('无法复制，请手动选择', 1); }
}

let _modalKey = null;
let _modalFocus = null;
let _modalInert = [];
function closeModal() {
  const el = $('#modal-root');
  if (el) el.remove();
  if (_modalKey) { document.removeEventListener('keydown', _modalKey); _modalKey = null; }
  _modalInert.forEach(([node, prior]) => { node.inert = prior; });
  _modalInert = [];
  const focus = _modalFocus?.isConnected ? _modalFocus : document.querySelector('#member-detail:not(.hidden) #md-close');
  if (focus && el) focus.focus({preventScroll:true});
  _modalFocus = null;
  document.body.classList.remove('modal-open');
}
function openModal(title, bodyHtml, opts) {
  closeModal();
  _modalFocus = document.activeElement;
  _modalInert = [...document.querySelectorAll('#layout')].map(node => [node, node.inert]);
  _modalInert.forEach(([node]) => { node.inert = true; });
  document.body.classList.add('modal-open');
  const wide = opts && opts.wide;
  const drawer = opts && opts.drawer;
  const root = document.createElement('div');
  root.id = 'modal-root';
  root.className = 'modal-root';
  root.innerHTML = `<div class="modal ${wide ? 'wide' : ''} ${drawer ? 'drawer' : ''}" role="dialog" aria-modal="true" aria-labelledby="modal-title" tabindex="-1">
    <div class="modal-head"><h3 id="modal-title">${esc(title)}</h3>
      <button class="btn sm" type="button" id="modal-close">关闭</button></div>
    <div class="modal-body">${bodyHtml}</div></div>`;
  document.body.appendChild(root);
  const box = root.querySelector('.modal');
  const focusables = () => [...box.querySelectorAll('a[href],button,input,select,textarea,summary')]
    .filter((x) => !x.disabled && x.offsetParent !== null);
  _modalKey = (e) => {
    if (e.key === 'Escape') { closeModal(); return; }
    if (e.key !== 'Tab') return;
    const list = focusables();
    if (!list.length) { e.preventDefault(); box.focus(); return; }
    const first = list[0]; const last = list[list.length - 1];
    if (!box.contains(document.activeElement)) { e.preventDefault(); first.focus(); return; }
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  document.addEventListener('keydown', _modalKey);
  root.addEventListener('click', (e) => { if (e.target === root) closeModal(); });
  $('#modal-close').onclick = closeModal;
  (focusables()[0] || box).focus();
  return root;
}

/* ---------------- shell ---------------- */
function buildNav() {
  $('#nav').innerHTML = NAV.map(g => `<a class="nav-item" href="#/${g.items[0].id}" data-page="${g.items[0].id}"><span class="ic" aria-hidden="true">${workspaceIcon(g.items[0].id)}</span><span>${esc(g.group)}</span></a>`).join('');
}
function updateWorkspaceNav(page) {
  const group = NAV.find(g => g.items.some(it => it.id === page)) || NAV[0];
  document.querySelectorAll('#nav .nav-item').forEach(n => {
    const active = n.dataset.page === group.items[0].id;
    n.classList.toggle('active', active);
    if (active) n.setAttribute('aria-current','page'); else n.removeAttribute('aria-current');
  });
  $('#subnav').innerHTML = group.items.map(it => `<a href="#/${it.id}" ${it.id === page ? 'class="active" aria-current="page"' : ''}>${esc(it.label)}</a>`).join('');
  setWorkspaceMenu(false);
}
function navMeta(id) {
  for (const g of NAV) for (const it of g.items) if (it.id === id) return it;
  return NAV[0].items[0];
}
function routeFromHash() {
  return (location.hash || '').replace(/^#\/?/, '') || 'dashboard';
}
function go(route) {
  route = String(route || 'dashboard').replace(/^#\/?/, '');
  if (route.split('?')[0] === 'tgrequests') {
    route = 'tgbot?section=groups';
    toast('Web 关联审核已移除；TG 换绑在绑定群中审核');
  }
  const page = route.split('?')[0] || 'dashboard';
  if (page === state.page && state.pageReady && ['settings','tgbot'].includes(page) && document.querySelector('.config-workspace')) {
    state.route = route;
    setWorkspaceMenu(false);
    activateConfigSection(new URLSearchParams(route.split('?')[1]).get('section'));
    return;
  }
  if (!configCanLeave() || (typeof membersCanLeave === 'function' && !membersCanLeave())) { history.replaceState(null, '', '#/' + (state.route || state.page)); return; }
  if (typeof membersDispose === 'function') membersDispose();
  state.page = page;
  state.route = route;
  state.pageReady = false;
  stopEnrollPoll();
  closeModal();
  const meta = navMeta(page);
  updateWorkspaceNav(page);
  $('#page-title').textContent = meta.label;
  $('#page-sub').textContent = meta.sub;
  if (location.hash !== '#/' + route) location.hash = '#/' + route;
  const pending = renderPage(page);
  connectLive(page);
  return pending;
}
window.addEventListener('hashchange', () => {
  const route = routeFromHash();
  if (route !== state.route) go(route);
});
async function renderPage(page, manual, liveUpdate, sourceContext) {
  // Completion of an old action must not repaint the workspace we left.
  if (page !== state.page || (sourceContext && !sourceContext.isCurrent())) return;
  if (liveUpdate) { scheduleLiveFlush(); return; }
  if (manual && (!configCanLeave() || (typeof membersCanLeave === 'function' && !membersCanLeave()))) return;
  if (manual && typeof membersDispose === 'function') membersDispose();
  const fn = PAGES[page];
  if (!fn) { $('#view').innerHTML = '<div class="empty">页面不存在</div>'; return; }
  state.renderVersion = (state.renderVersion || 0) + 1;
  state.pageReady = false;
  const context = pageContext(page);
  try {
    await fn(context);
    if (!context.isCurrent()) return;
    state.pageReady = true;
    const retry = $('#retry-page');
    if (retry) retry.onclick = () => renderPage(page, true);
    $('#last-updated').textContent = '最近更新: ' + new Date().toLocaleTimeString();
    if (manual) toast('已刷新');
    scheduleLiveFlush();
  } catch (e) {
    if (!context.isCurrent()) return;
    $('#view').innerHTML = pageError(e);
    const btn = $('#retry-page');
    if (btn) btn.onclick = () => renderPage(page, true);
    toast('加载失败: ' + e.message, 1);
  }
}

/* ---------------- pages ---------------- */
PAGES.dashboard = async (context = pageContext('dashboard')) => {
  // Every page paints a placeholder before awaiting. Without it a slow first
  // request leaves the previous page's content on screen, which reads as a
  // click that did nothing.
  renderView(pageLoading(), context);
  const [sessions, pipe, nodes, overview] = await Promise.all([
    api('/api/emby/sessions'),
    api('/api/pipeline').catch(() => ({ available: false })),
    api('/api/nodes'),
    api('/api/stats/overview?days=30'),
  ]);
  if (!context.isCurrent()) return;
  PAGE_MODELS.dashboard = {sessions, pipe, nodes, overview};
  paintDashboard(PAGE_MODELS.dashboard, context);
};

function paintDashboard(model, context) {
  if (!context.isCurrent()) return;
  const {sessions, pipe, nodes, overview} = model;
  const online = nodes.filter((n) => n.available).length;
  const d = pipe.available ? pipe.data : {};
  const queues = d.queues || [];
  const queued = queues.reduce((a, q) => a + (q.items || 0), 0);
  const limited = (d.quota || []).filter((q) => q.state !== 'ok').length;
  const alerts = d.alerts || [];
  const mem = (overview && overview.members) || {};
  const expiring = (overview && overview.expiring_7d) || [];
  const exhaustedN = mem.exhausted || 0;

  renderView(`
    <div class="stat-grid">
      ${stat('☺', mem.total || 0, '成员', `${mem.active || 0} 正常 · ${mem.expired || 0} 过期`)}
      ${stat('⛁', `${online} / ${nodes.length}`, '在线节点', nodes.length ? '推流节点健康状态' : '尚未配置节点')}
      ${stat('▶', sessions.length, '当前播放', sessions.length ? '正在进行的会话' : '暂无活跃会话')}
      ${stat('⇄', queued, '管线待处理', pipe.available ? '整理与上传队列' : '快照不可用')}
      ${stat('⚠', alerts.length + limited + expiring.length + exhaustedN, '待处理事项', limited ? `${limited} 个上传身份受限` : '系统关键状态')}
    </div>
    <div class="dashboard-shortcuts"><a class="btn" href="#/members">管理用户</a><a class="btn" href="#/requests">处理求片</a><a class="btn" href="#/pipeline">查看管线</a></div>
    <div class="dashboard-focus">
      ${card('正在播放', sessions.length ? `${sessions.length} 个会话 · 节点实测速率` : '暂无活跃会话',
        sessions.length ? `<div class="play-grid">${sessions.map(playCard).join('')}</div>` : '<div class="empty">当前没有播放会话</div>')}
      ${card('待处理事项', '账号与系统异常优先', `<div class="card-body">
        ${alerts.map(a => `<p><span class="tag warn">${esc(a.level)}</span> ${esc(a.message)}</p>`).join('')}
        ${limited ? `<p><a href="#/pipeline">${limited} 个上传身份受限 →</a></p>` : ''}
        ${queued ? `<p><a href="#/pipeline">管线队列 ${queued} 项 →</a></p>` : ''}
        ${expiring.length ? `<p><a href="#/members?expiring=soon">7 天内到期 ${expiring.length} 人 →</a></p>` : ''}
        ${mem.exhausted || mem.expired ? `<p><a href="#/members">超额 ${esc(mem.exhausted || 0)} · 过期 ${esc(mem.expired || 0)} →</a></p>` : ''}
        ${!alerts.length && !limited && !queued && !expiring.length && !mem.exhausted && !mem.expired ? '<p class="muted">当前没有待处理事项</p>' : ''}
        <a href="#/nodes">节点状态</a> · <a href="#/audit">审计日志</a>
      </div>`)}
    </div>`, context);
}

PAGES.library = async (context = pageContext('library')) => {
  $('#view').innerHTML = pageLoading();
  const [libs, latest] = await Promise.all([api('/api/emby/libraries'), api('/api/emby/latest?limit=12')]);
  if (!context.isCurrent()) return;
  const total = libs.reduce((a, l) => a + (l.items || 0), 0);
  const kinds = new Set(libs.map((l) => l.type)).size;
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('▤', libs.length, '媒体库数量', '已配置的库')}
      ${stat('≡', total.toLocaleString(), '媒体条目', '电影与剧集合计')}
      ${stat('⛁', kinds, '库类型', '按内容类型划分')}
    </div>
    ${card('最新入库', '最近 12 部', latest.length ? `<div class="poster-grid">${latest.map(posterTile).join('')}</div>` : '<div class="empty">暂无最近入库</div>')}
    ${tableCard('媒体库', `${libs.length} 个库`, ['名称', '类型', '条目数', '存储位置'],
      libs.map((l) => `<tr><td>${esc(l.name)}</td>
        <td><span class="tag idle">${esc(l.type)}</span></td>
        <td>${l.items == null ? '<span class="muted">-</span>' : Number(l.items).toLocaleString()}</td>
        <td>${esc(l.locations)} 个路径</td></tr>`).join(''))}`;
};

PAGES.imports = async (context = pageContext('imports')) => {
  $('#view').innerHTML = pageLoading();
  const js = await api('/api/imports?limit=50');
  if (!context.isCurrent()) return;
  $('#view').innerHTML = `
    ${card('新建导入', '提交网盘链接或云盘目录',
      `<div class="card-body"><div class="toolbar">
        <select id="imp-kind" aria-label="导入类型"><option value="drive-link">网盘链接</option><option value="cloud-drive">云盘目录</option></select>
        <input id="imp-src" aria-label="导入来源" placeholder="链接 / 目录引用" style="flex:1;min-width:240px">
        <input id="imp-cat" aria-label="导入分类" placeholder="分类(可选)" style="width:120px">
        <button class="btn primary" id="imp-go">提交</button>
      </div></div>`)}
    ${tableCard('导入任务', `${js.length} 个`, ['ID', '类型', '来源', '状态', '进度', ''],
      js.map((j) => {
        const p = Math.round((j.progress || 0) * 100);
        const cls = j.state === 'done' ? 'ok' : (j.state === 'failed' ? 'bad' : 'idle');
        return `<tr><td>${esc(j.id)}</td><td>${esc(j.kind)}</td>
          <td>${esc((j.source_ref || '').slice(0, 44))}</td>
          <td><span class="tag ${cls}">${esc(j.state)}</span></td>
          <td><span class="bar"><i style="width:${p}%"></i></span> ${j.items_done}/${j.items_total}</td>
          <td>${(j.state === 'queued' || j.state === 'running')
            ? `<button class="btn sm danger" onclick="cancelImport(${jsArg(j.id)})">取消</button>` : ''}</td></tr>`;
      }).join(''))}`;
  $('#imp-go').onclick = submitImport;
};
async function submitImport() {
  const actionContext = pageContext('imports');
  const src = $('#imp-src').value.trim();
  if (!src) return toast('请输入来源', 1);
  const button = $('#imp-go');
  if (button.disabled) return;
  button.disabled = true;
  const context = pageContext('imports');
  try {
    await api('/api/imports', { method: 'POST', body: JSON.stringify({
      kind: $('#imp-kind').value, source_ref: src, category: $('#imp-cat').value.trim() }) });
    toast('任务已提交'); if (context.isCurrent()) renderPage('imports', false, false, actionContext);
  } catch (e) { toast('提交失败: ' + e.message, 1); }
  finally { button.disabled = false; }
}
async function cancelImport(id) {
  const actionContext = pageContext('imports');
  try { await api(`/api/imports/${id}/cancel`, { method: 'POST' }); toast('已取消'); renderPage('imports', false, false, actionContext); }
  catch (e) { toast('取消失败: ' + e.message, 1); }
}

PAGES.nodes = async (context = pageContext('nodes')) => {
  renderView(pageLoading(), context);
  const [ns, log, dispatch, st] = await Promise.all([
    api('/api/nodes'),
    api('/api/dispatch/log?limit=20'),
    api('/api/settings/dispatch'),
    api('/api/settings'),
  ]);
  if (!context.isCurrent()) return;
  PAGE_MODELS.nodes = {ns, log, dispatch, st};
  paintNodes(PAGE_MODELS.nodes, context);
};

function paintNodes(model, context) {
  if (!context.isCurrent()) return;
  const {ns, log, dispatch, st} = model;
  const previous = new Set((state.nodes || []).map((n) => n.name));
  state.nodes = ns;
  const online = ns.filter((n) => n.available).length;
  const streams = ns.reduce((a, n) => a + (n.active_streams || 0), 0);
  const egress = egressSummary(ns);
  const policyLabel = dispatch.policy === 'affinity' ? '文件亲和' : '最低负载';
  const panelSet = !!(st.integration || {}).panel_public_url;

  renderView(`
    <div class="stat-grid">
      ${stat('⛁', `${online} / ${ns.length}`, '在线节点', '可用于分发')}
      ${stat('▶', streams, '活跃流', '所有节点合计')}
      ${stat('⇅', egress.text, '出口带宽', egress.sub)}
      ${stat('⚖', policyLabel, '调度策略', dispatch.policy === 'affinity'
        ? `占用率阈值 ${Math.round(dispatch.load_threshold * 100)}%` : '按容量占用率择优')}
    </div>
    ${panelSet ? '' : card('⚠ 尚未填写面板对外地址', '节点安装时需要用它回连面板取配置',
      `<div class="card-body"><div class="muted">请先到「系统设置 → 接入方式」填写面板对外地址，否则无法生成节点安装命令。</div></div>`)}
    ${card('新增节点', '只需名称；地址由节点安装后自动上报',
      `<div class="card-body"><div class="toolbar">
        <input id="nd-name" aria-label="节点名称" placeholder="节点名称 如 node-a" style="width:160px">
        <input id="nd-capacity" type="number" min="1" value="100" style="width:100px" title="并发容量（可选）">
        <button class="btn primary" id="nd-go">添加</button>
      </div>
      <div class="muted" style="margin-top:8px">添加后会给出一条安装命令。节点回连后才显示真实对外地址。</div>
      </div>`)}
    ${ns.length ? ns.map(nodeCard).join('') : card('推流节点', '尚未配置',
      '<div class="card-body"><div class="empty">还没有节点</div></div>')}
    ${tableCard('最近分发', '302 调度记录', ['时间', '节点', '占用', '候选', '策略', '请求'],
      log.slice().reverse().map((e) => `<tr><td>${new Date(e.ts * 1000).toLocaleTimeString()}</td>
        <td>${esc(e.node || '-')}</td>
        <td>${e.utilisation == null ? '-' : Math.round(e.utilisation * 100) + '%'}</td>
        <td>${esc(e.candidates)}</td>
        <td><span class="tag idle">${esc(e.reason || e.policy || '-')}</span></td>
        <td>${esc((e.context || '').slice(0, 44))}</td></tr>`).join(''))}`, context);
  bindAsyncButton('nd-go', addNode);
  ns.filter((n) => !context.live || !previous.has(n.name)).forEach((n) => fillNodeMounts(n));
}

/* 一个节点 = 一张卡：健康、媒体根、缓存、签名、安装命令全在这里。
   这些都是「这台机器」的属性，放全局设置里是错的。 */
function nodeCard(n) {
  const pools = n.pools || [];
  const health = n.available ? '<span class="tag ok">可用</span>'
    : (n.manually_disabled ? '<span class="tag idle">已下线</span>'
                           : '<span class="tag bad">不健康</span>');
  const poolRows = pools.length
    ? pools.map((p, i) => `<tr>
        <td>${esc(p.name)}</td><td><code>${esc(p.emby_prefix)}</code></td>
        <td><code>${esc(p.node_path)}</code></td>
        <td><code>${esc(p.rclone_remote)}</code></td>
        <td><code>${esc(p.url_prefix)}</code></td>
        <td><button class="btn sm danger" onclick="delPool(${jsArg(n.name)},${i})">删除</button></td>
      </tr>`).join('')
    : '';
  return card(`⛁ ${n.name}`,
    `${n.active_streams}/${n.capacity} 路 · ${Math.round((n.utilisation || 0) * 100)}% · 整网卡出口以实测采样为准`,
    `<div class="card-body">
      <div class="toolbar" style="margin-bottom:10px">
        ${health}
        ${egressCell(n)}
        <span class="muted">${esc(n.base_url)}</span>
        <span style="flex:1"></span>
        <button class="btn sm" onclick="nodeCtl(${jsArg(n.name)},'${n.manually_disabled ? 'enable' : 'disable'}')">${n.manually_disabled ? '上线' : '下线'}</button>
        <button class="btn sm" onclick="editNodeCapacity(${jsArg(n.name)},${n.capacity})">改容量</button>
        <button class="btn sm danger" onclick="deleteNode(${jsArg(n.name)})">删除</button>
      </div>

      <div class="sub" style="margin:12px 0 4px"><b>媒体根映射</b> — Emby 里的路径对应节点上的哪个目录</div>
      ${poolRows
        ? `<div class="table-scroll"><table><thead><tr><th>名称</th><th>Emby 路径</th><th>节点路径</th><th>rclone remote</th><th>URL 前缀</th><th></th></tr></thead><tbody>${poolRows}</tbody></table></div>`
        : '<div class="empty">未配置媒体根 — 该节点当前无法提供任何文件</div>'}
      <div class="toolbar" style="margin-top:8px">
        <input id="pl-name-${esc(n.name)}" aria-label="媒体根名称" placeholder="名称 main" style="width:90px">
        <input id="pl-emby-${esc(n.name)}" aria-label="Emby 路径" placeholder="Emby 路径 /media" style="width:150px">
        <input id="pl-path-${esc(n.name)}" aria-label="节点路径" placeholder="节点路径 /mnt/gdrive/Media" style="flex:1;min-width:170px">
        <input id="pl-remote-${esc(n.name)}" aria-label="rclone remote" placeholder="remote rc2:Media" style="width:150px">
        <input id="pl-url-${esc(n.name)}" aria-label="URL 前缀" placeholder="URL 前缀 /s/main" style="width:120px">
        <button class="btn" onclick="addPool(${jsArg(n.name)})">添加媒体根</button>
      </div>

      <div class="sub" style="margin:14px 0 4px"><b>存储与安全</b></div>
      <div class="form-row"><label for="nc-dir-${esc(n.name)}">缓存目录</label>
        <input id="nc-dir-${esc(n.name)}" value="${esc(n.cache_dir || '')}" placeholder="/var/cache/mediadeck"></div>
      <div class="form-row"><label for="nc-size-${esc(n.name)}">缓存上限</label>
        <input id="nc-size-${esc(n.name)}" value="${esc(n.cache_size || '')}" placeholder="2T" style="width:120px">
        <span class="muted">别超过该盘可用空间</span></div>
      <div class="form-row"><label>签名密钥</label>
        <span class="${n.sign_secret_set ? 'tag ok' : 'tag bad'}">${n.sign_secret_set ? '已设置' : '未设置 · 链接永久公开'}</span>
        <span class="muted">${esc(n.sign_secret_masked || '')}</span>
        <button class="btn sm" onclick="rotateNodeSecret(${jsArg(n.name)})">重置密钥</button></div>
      <div class="form-row"><label for="nc-argd-${esc(n.name)}">签名参数</label>
        <input id="nc-argd-${esc(n.name)}" value="${esc(n.sign_arg_digest || 'md5')}" style="width:90px">
        <input id="nc-arge-${esc(n.name)}" aria-label="签名有效期参数名" value="${esc(n.sign_arg_expires || 'expires')}" style="width:110px">
        <span class="muted">节点 nginx 用的参数名（已有站点常用 k / e）</span></div>
      <div class="form-row"><label for="nc-ttl-${esc(n.name)}">链接有效期</label>
        <input id="nc-ttl-${esc(n.name)}" type="number" min="60" value="${esc(n.sign_ttl_seconds || 21600)}" style="width:120px">
        <span class="muted">秒</span></div>
      ${n.legacy_config ? `<div class="form-row"><label>旧式配置</label>
        <span class="tag warn">该节点仍保存独立 rclone.conf</span>
        <button class="btn sm" onclick="migrateNodeStorage(${jsArg(n.name)})">迁移到全局挂载</button></div>` : ''}
      <div class="form-row"><label>全局挂载</label>
        <div id="nmounts-${esc(n.name)}" data-live-preserve class="muted">加载中…</div></div>
      <div class="form-row"><label>接入状态</label>
        ${n.enrolled ? `<span class="tag ok">已接入</span> <span class="muted">${esc(fmtAgeTs(n.first_seen_at))} · ${esc(n.enrolled_host || n.base_url)}</span>`
                      : '<span class="tag warn">待接入</span>'}</div>
      <div class="toolbar">
        <button class="btn primary" onclick="saveNodeStorage(${jsArg(n.name)})">保存</button>
        <button class="btn" onclick="showEnroll(${jsArg(n.name)})">获取安装命令</button>
      </div>
      <div id="enroll-${esc(n.name)}" data-live-preserve style="margin-top:10px"></div>
    </div>`);
}

async function addPool(name) {
  const actionContext = pageContext('nodes');
  const g = (k) => ($(`#pl-${k}-${CSS.escape(name)}`) || {}).value || '';
  const node = (state.nodes || []).find((n) => n.name === name);
  if (!node) return;
  const pools = (node.pools || []).slice();
  pools.push({
    name: g('name').trim(), emby_prefix: g('emby').trim(),
    node_path: g('path').trim(), rclone_remote: g('remote').trim(),
    url_prefix: g('url').trim() || ('/s/' + g('name').trim()),
  });
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify({ pools }) });
    toast('媒体根已添加'); renderPage('nodes', false, false, actionContext);
  } catch (e) { toast('添加失败: ' + e.message, 1); }
}
async function delPool(name, index) {
  const actionContext = pageContext('nodes');
  const node = (state.nodes || []).find((n) => n.name === name);
  if (!node) return;
  const target = (node.pools || [])[index];
  if (!target || !confirm(`删除节点 ${name} 的媒体根 ${target.name || index + 1}（${target.emby_prefix || ''}）？该路径将不再由此节点提供播放。`)) return;
  const pools = (node.pools || []).filter((_, i) => i !== index);
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify({ pools }) });
    toast('已删除'); renderPage('nodes', false, false, actionContext);
  } catch (e) { toast('删除失败: ' + e.message, 1); }
}
async function saveNodeStorage(name) {
  const actionContext = pageContext('nodes');
  const g = (k) => ($(`#nc-${k}-${CSS.escape(name)}`) || {}).value || '';
  const mountIds = [...document.querySelectorAll(`.nmount-${CSS.escape(name)}:checked`)].map((x) => x.value);
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify({
      cache_dir: g('dir').trim(), cache_size: g('size').trim(),
      sign_arg_digest: g('argd').trim(), sign_arg_expires: g('arge').trim(),
      sign_ttl_seconds: parseInt(g('ttl'), 10) || 21600,
      ...($(`#nmounts-${CSS.escape(name)}`)?.dataset.loaded === 'true' ? {mount_ids: mountIds} : {}),
    }) });
    toast('已保存'); renderPage('nodes', false, false, actionContext);
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
async function rotateNodeSecret(name) {
  const actionContext = pageContext('nodes');
  if (!confirm(`重置 ${name} 的签名密钥？\n\n已发出的播放链接会立即失效，且必须重新在节点上执行安装命令。`)) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}/rotate-secret`, { method: 'POST' });
    toast('密钥已重置，请重新部署该节点'); renderPage('nodes', false, false, actionContext);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function editRcloneConf(name) {
  const actionContext = pageContext('nodes');
  const text = prompt(`粘贴该节点使用的 rclone.conf 全文\n（建议为节点单独建 OAuth 身份，避免和主机抢配额）`);
  if (text === null) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify({ rclone_conf: text }) });
    toast('已保存'); renderPage('nodes', false, false, actionContext);
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
function stopEnrollPoll() {
  state.enrollVersion = (state.enrollVersion || 0) + 1;
  if (state.enrollTimer) { clearInterval(state.enrollTimer); state.enrollTimer = null; }
}
async function showEnroll(name) {
  stopEnrollPoll();
  const box = $(`#enroll-${CSS.escape(name)}`);
  if (!box) return;
  box.textContent = '生成中…';
  const version = state.enrollVersion;
  const current = () => state.enrollVersion === version && box.isConnected && state.page === 'nodes';
  let polling = false;
  const paint = async () => {
    if (!current() || polling) return;
    polling = true;
    try {
      const r = await api(`/api/nodes/${encodeURIComponent(name)}/enroll`);
      if (!current()) return;
      const enrolled = r.enrolled;
      box.innerHTML = `
        <div class="toolbar" style="margin-bottom:6px">
          ${enrolled ? `<span class="tag ok">已接入</span> <span class="muted">${esc(fmtAgeTs(r.first_seen_at))} · ${esc(r.enrolled_host || '')}</span>`
                     : '<span class="tag warn">待接入</span>'}
          <button class="btn sm" type="button" id="copy-enroll-${esc(name)}">复制命令</button>
          <button class="btn sm" type="button" id="rotate-enroll-${esc(name)}">重新生成安装命令</button>
        </div>
        <div class="muted" style="margin-bottom:6px">在新机器上以 root 执行。命令不会自动运行。</div>
        <pre class="codeblock">${esc(r.command)}</pre>
        ${(r.warnings || []).map((w) => `<div class="tag warn" style="margin-top:6px">${esc(w)}</div>`).join('')}
        <div class="muted" style="margin-top:6px">重新生成后，旧命令立即失效。</div>`;
      const copyBtn = $(`#copy-enroll-${CSS.escape(name)}`);
      if (copyBtn) copyBtn.onclick = () => copyText(r.command);
      const rotBtn = $(`#rotate-enroll-${CSS.escape(name)}`);
      if (rotBtn) rotBtn.onclick = () => rotateEnroll(name);
      if (enrolled) stopEnrollPoll();
    } catch (e) {
      if (!current()) return;
      box.innerHTML = `<span class="tag bad">生成失败</span> ${esc(e.message)}`;
      stopEnrollPoll();
    } finally { polling = false; }
  };
  await paint();
  if (current()) state.enrollTimer = setInterval(paint, 4000);
}
async function rotateEnroll(name) {
  if (!confirm('重新生成后，旧的安装命令立即失效。继续？')) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}/rotate-enroll`, { method: 'POST' });
    toast('已重新生成');
    showEnroll(name);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function nodeCtl(name, action) {
  const actionContext = pageContext('nodes');
  try { await api(`/api/nodes/${encodeURIComponent(name)}/${action}`, { method: 'POST' });
    toast(action === 'disable' ? '已下线' : '已上线'); renderPage('nodes', false, false, actionContext);
  } catch (e) { toast('操作失败: ' + e.message, 1); }
}
async function addNode() {
  const actionContext = pageContext('nodes');
  const body = {
    name: $('#nd-name').value.trim(),
    capacity: parseFloat($('#nd-capacity').value) || 100,
  };
  if (!body.name) return toast('请填写节点名称', 1);
  try {
    const created = await api('/api/nodes', { method: 'POST', body: JSON.stringify(body) });
    toast('节点已登记');
    await renderPage('nodes', false, false, actionContext);
    if (actionContext.isCurrent()) showEnroll(created.name);
  } catch (e) { toast('添加失败: ' + e.message, 1); }
}
async function fillNodeMounts(n) {
  const box = $(`#nmounts-${CSS.escape(n.name)}`);
  if (!box) return;
  try {
    const mounts = await api('/api/storage/mounts');
    box.dataset.loaded = 'true';
    if (!mounts.length) { box.textContent = '还没有全局挂载，请先到「存储管理」添加'; return; }
    const chosen = new Set(n.mount_ids || []);
    box.innerHTML = mounts.map((m) => `<label style="margin-right:12px">
      <input type="checkbox" class="nmount-${esc(n.name)}" value="${esc(m.name)}" ${chosen.has(m.name) ? 'checked' : ''}>
      ${esc(m.name)} <span class="muted">(${esc(m.remote)})</span></label>`).join('');
  } catch (e) {
    box.textContent = '无法读取全局挂载: ' + e.message;
  }
}
async function migrateNodeStorage(name) {
  const actionContext = pageContext('nodes');
  if (!confirm(`把 ${name} 的旧式 rclone.conf 标记为已迁移？\n\n独立配置会保留，但之后请改用全局挂载列表。`)) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify({ mount_ids: [] }) });
    toast('已切换为全局挂载模式（旧配置仍保留，不会静默删除）');
    renderPage('nodes', false, false, actionContext);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function editNodeCapacity(name, current) {
  const actionContext = pageContext('nodes');
  const value = prompt(`设置 ${name} 的并发容量（最多同时承载多少路播放）`, current);
  if (value === null) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify({ capacity: parseFloat(value) }) });
    toast('容量已更新'); renderPage('nodes', false, false, actionContext);
  } catch (e) { toast('更新失败: ' + e.message, 1); }
}
async function deleteNode(name) {
  const actionContext = pageContext('nodes');
  if (!confirm(`确认删除节点 ${name}？该节点将不再参与播放分发。`)) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, { method: 'DELETE' });
    toast('节点已删除'); renderPage('nodes', false, false, actionContext);
  } catch (e) { toast('删除失败: ' + e.message, 1); }
}

PAGES.pipeline = async (context = pageContext('pipeline')) => {
  renderView(pageLoading(), context);
  const p = await api('/api/pipeline').catch(() => ({available:false}));
  paintPipeline(p, context);
};
function paintPipeline(p, context) {
  if (!context.isCurrent()) return;
  if (!p.available) { renderView(`<div class="card"><div class="empty">管线快照不可用</div></div>`, context); return; }
  const d = p.data, f = d.fallback || {};
  const pct = f.capacity_bytes ? Math.round((f.bytes / f.capacity_bytes) * 100) : 0;
  renderView(`
    <div class="stat-grid">
      ${(d.queues || []).map((q) => stat('⇄', q.items, q.name, `${fmtBytes(q.bytes)} · 最老 ${fmtAge(q.oldest_age_seconds)}`)).join('')}
      ${stat('⛃', fmtBytes(f.bytes), '本地应急仓', `${f.items || 0} 个文件 · ${pct}% 容量`)}
    </div>
    ${tableCard('上传身份配额', `快照 ${Math.round(p.snapshot_age_seconds)}s 前${p.stale ? ' · 已过期' : ''}`,
      ['身份', '状态', '受限起始'],
      (d.quota || []).map((q) => `<tr data-live-key="quota:${esc(q.identity)}"><td>${esc(q.identity)}</td>
        <td><span class="tag ${q.state === 'ok' ? 'ok' : 'bad'}">${esc(q.state)}</span></td>
        <td>${esc(q.limited_since || '-')}</td></tr>`).join(''))}
    ${card('告警', '管线异常', (d.alerts || []).length
      ? `<div class="card-body flush">${d.alerts.map((a) =>
          `<div class="list-row"><div class="t">${esc(a.message)}</div>
           <span class="tag ${a.level === 'warn' ? 'warn' : 'idle'}">${esc(a.level)}</span></div>`).join('')}</div>`
      : `<div class="empty">无告警</div>`)}`, context);
}

PAGES.mounts = async (context = pageContext('mounts')) => {
  renderView(pageLoading(), context);
  const m = await api('/api/mounts').catch(() => ({available:false}));
  paintMounts(m, context);
};
function paintMounts(m, context) {
  if (!context.isCurrent()) return;
  if (!m.available) {
    renderView(`<div class="card"><div class="empty">挂载快照不可用</div></div>`, context);
    return;
  }
  const d = m.data, ms = d.mounts || [];
  const alive = ms.filter((x) => x.alive).length;
  const stuck = ms.reduce((a, x) => a + (x.stuck_processes || 0), 0);
  const cache = ms.reduce((a, x) => a + (x.cache_bytes || 0), 0);
  renderView(`
    <div class="stat-grid">
      ${stat('⛃', `${alive} / ${ms.length}`, '挂载存活', '可正常读取目录')}
      ${stat('⚠', stuck, '阻塞进程', stuck ? '存在不可中断 I/O' : '无卡死进程')}
      ${stat('⛁', fmtBytes(cache), '缓存占用', 'VFS 本地缓存合计')}
    </div>
    ${tableCard('存储挂载', `快照 ${Math.round(m.snapshot_age_seconds)}s 前${m.stale ? ' · 已过期' : ''}`,
      ['挂载', '类型', '状态', '探测耗时', '阻塞', '缓存', '可用空间'],
      ms.map((x) => {
        const cachePct = x.cache_limit_bytes
          ? Math.round((x.cache_bytes / x.cache_limit_bytes) * 100) : null;
        return `<tr data-live-key="mount:${esc(x.label)}"><td>${esc(x.label)}<div class="s muted">${esc((x.options || []).join(','))}</div></td>
          <td>${esc(x.kind)}</td>
          <td><span class="tag ${x.alive ? 'ok' : 'bad'}">${x.alive ? '正常' : '异常'}</span></td>
          <td>${x.readdir_ms == null ? '<span class="muted">超时</span>' : x.readdir_ms + ' ms'}</td>
          <td>${x.stuck_processes ? `<span class="tag bad">${x.stuck_processes}</span>` : '<span class="muted">0</span>'}</td>
          <td>${x.cache_bytes == null ? '<span class="muted">-</span>'
            : `${fmtBytes(x.cache_bytes)}${cachePct == null ? '' : ` <span class="muted">(${cachePct}%)</span>`}`}</td>
          <td>${x.fs_free_bytes == null ? '<span class="muted">-</span>' : fmtBytes(x.fs_free_bytes)}</td></tr>`;
      }).join(''))}
    ${card('告警', '存储层异常', (d.alerts || []).length
      ? `<div class="card-body flush">${d.alerts.map((a) =>
          `<div class="list-row"><div class="t">${esc(a.message)}</div>
           <span class="tag ${a.level === 'warn' ? 'warn' : 'bad'}">${esc(a.level)}</span></div>`).join('')}</div>`
      : `<div class="empty">无告警</div>`)}`, context);
}

PAGES.tasks = async (context = pageContext('tasks')) => {
  renderView(pageLoading(), context);
  const t = await api('/api/tasks').catch(() => ({available:false}));
  paintTasks(t, context);
};
function paintTasks(t, context) {
  if (!context.isCurrent()) return;
  if (!t.available) {
    renderView(`<div class="card"><div class="empty">调度快照不可用</div></div>`, context);
    return;
  }
  const d = t.data, ts = d.tasks || [];
  const failed = ts.filter((x) => x.last_status === 'failed').length;
  const disabled = ts.filter((x) => !x.enabled).length;
  const statusCls = (st) => (st === 'ok' ? 'ok' : st === 'failed' ? 'bad'
    : st === 'unknown' ? 'idle' : 'warn');
  renderView(`
    <div class="stat-grid">
      ${stat('⏱', ts.length, '任务总数', '快照中的定时任务')}
      ${stat('⚠', failed, '当前失败', failed ? '最近一次运行失败' : '全部正常')}
      ${stat('⏸', disabled, '已禁用', '未纳入调度')}
    </div>
    ${tableCard('定时任务', `快照 ${Math.round(t.snapshot_age_seconds)}s 前${t.stale ? ' · 已过期' : ''}`,
      ['任务名', '计划', '状态', '上次运行', '耗时', '连续失败'],
      ts.map((x) => {
        const ageSec = x.last_run ? (Date.now() / 1000) - x.last_run : null;
        const age = ageSec == null ? '-' : fmtAge(ageSec) + '前';
        const dur = x.last_duration_ms == null
          ? '<span class="muted">-</span>' : `${esc(x.last_duration_ms)} ms`;
        const streak = x.failure_streak > 0
          ? `<span class="tag bad">${esc(x.failure_streak)}</span>`
          : '<span class="muted">0</span>';
        const st = x.last_status || 'unknown';
        return `<tr data-live-key="task:${esc(x.name)}"><td>${esc(x.name)}${x.enabled ? '' : '<div class="s muted">已禁用</div>'}</td>
          <td>${esc(x.schedule)}</td>
          <td><span class="tag ${statusCls(st)}">${esc(st)}</span></td>
          <td>${esc(age)}</td>
          <td>${dur}</td>
          <td>${streak}</td></tr>`;
      }).join(''))}
    ${card('告警', '调度异常', (d.alerts || []).length
      ? `<div class="card-body flush">${d.alerts.map((a) =>
          `<div class="list-row"><div class="t">${esc(a.message)}</div>
           <span class="tag ${a.level === 'warn' ? 'warn' : 'bad'}">${esc(a.level)}</span></div>`).join('')}</div>`
      : `<div class="empty">无告警</div>`)}`, context);
}

PAGES.settings = async (context = pageContext('settings')) => {
  $('#view').innerHTML = pageLoading();
  const s = await api('/api/settings');
  if (!context.isCurrent()) return;
  if (!s) { $('#view').innerHTML = `<div class="card"><div class="empty">设置加载失败</div></div>`; return; }
  const e = s.emby, d = s.dispatch, p = s.playback, ig = s.integration;
  const tg = await api('/api/settings/telegram').catch(() => ({}));
  if (!context.isCurrent()) return;
  const connected = e.enabled && e.api_key_set;
  const mapped = (s.nodes || []).filter((n) => (n.pools || []).length).length;
  $('#view').innerHTML = `
    ${s.mock_mode ? card('演示模式', '当前以 MEDIADECK_MOCK=1 运行',
      `<div class="card-body"><div class="muted">所有数据均为模拟值，保存的配置不会连接真实服务。</div></div>`) : ''}
    ${card('Emby 对接', connected ? '已连接' : '尚未连接 — 用户管理与媒体库依赖此配置',
      `<div class="card-body">
        <div class="form-row"><label for="em-url">服务器地址</label>
          <input id="em-url" value="${esc(e.url)}" placeholder="http://127.0.0.1:8096"></div>
        <div class="form-row"><label for="em-key">API Key</label>
          <input id="em-key" type="password" placeholder="${e.api_key_set ? esc(e.api_key_masked) + '（留空则不修改）' : '在 Emby 后台「高级 → API 密钥」创建'}"></div>
        <div class="form-row"><label for="em-timeout">请求超时</label>
          <input id="em-timeout" type="number" min="1" max="120" value="${esc(e.timeout_seconds)}" style="width:110px"> <span class="muted">秒</span></div>
        <div class="form-row"><label for="em-enabled">启用集成</label>
          <input id="em-enabled" type="checkbox" ${e.enabled ? 'checked' : ''}>
          <span class="muted">关闭后面板不再调用 Emby</span></div>
        <div class="form-row"><label for="em-verify">校验证书</label>
          <input id="em-verify" type="checkbox" ${e.verify_ssl ? 'checked' : ''}>
          <span class="muted">自签名证书请取消勾选</span></div>
        <div class="toolbar">
          <button class="btn" id="em-test">测试连接</button>
          <button class="btn primary" id="em-save">保存</button>
          <span id="em-result" class="muted">${connected ? '已配置' : '未配置'}</span>
        </div>
      </div>`)}
    ${card('接入方式', '你现有的 Emby 域名如何把播放分发到节点',
      `<div class="card-body">
        <div class="muted" style="margin-bottom:12px">
          用户仍然只访问原来的 Emby 地址，客户端不用改任何设置。
          只需在反代里把<b>播放请求</b>转给面板；Web 界面、刮削、图片、转码照旧直接走 Emby。
          <b>面板地址</b>同时也是节点装机时回连取配置的地址，必须填。
        </div>
        <div class="form-row"><label for="ig-panel">面板地址</label>
          <input id="ig-panel" value="${esc(ig.panel_public_url)}" placeholder="https://deck.example.com"></div>
        <div class="form-row"><label for="ig-emby">Emby 地址</label>
          <input id="ig-emby" value="${esc(ig.emby_public_url)}" placeholder="https://emby.example.com"></div>
        <div class="muted" style="margin:14px 0 8px">
          <b>TMDB</b>（可选）。填了之后成员求片会显示片名、年份和海报；
          不填也能用，求片只会记下 TMDB 编号，上片员照样能处理。
          <a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noreferrer">去申请 Key</a>
        </div>
        <div class="form-row"><label for="ig-tmdb">TMDB Key</label>
          <input id="ig-tmdb" type="password" autocomplete="new-password"
            placeholder="${ig.tmdb_api_key_set ? esc(ig.tmdb_api_key_masked) : '未配置'}">
          <span class="muted">${ig.tmdb_api_key_set ? '已配置，留空表示不修改' : '留空表示不启用'}</span></div>
        <div class="form-row"><label for="ig-tmdb-lang">TMDB 语言</label>
          <input id="ig-tmdb-lang" value="${esc(ig.tmdb_language || 'zh-CN')}" style="width:120px">
          <span class="muted">zh-CN / en-US / ja-JP</span></div>
        <div class="toolbar">
          <select id="ig-server" aria-label="反代服务器类型" style="width:120px">
            <option value="caddy">Caddy</option><option value="nginx">nginx</option>
          </select>
          <button class="btn" id="ig-show">生成反代配置</button>
          <button class="btn primary" id="ig-save">保存</button>
        </div>
        <div id="ig-out" style="margin-top:10px"></div>
      </div>`)}
    ${card('外部反代入口', '朋友用自己的域名反代时，让播放也留在他的域名上',
      `<div class="card-body">
        <div class="muted" style="margin-bottom:12px">
          登记之后，从该入口进来的播放会 302 到
          <code>https://对方域名/_n/&lt;节点&gt;/s/...</code>：签名原样保留，推流节点不用改任何配置。
          每个入口有独立凭据，普通设置只显示配置状态；点击「生成配置」后才显示完整凭据。
        </div>
        <div class="muted" style="margin-bottom:12px">
          对方 CDN <b>无法按路径分流</b>时，改填「推流域名 + 固定节点」：302 直接写成
          <code>https://推流域名/s/...</code>，对方只需两个域名各回源一个固定源站。
          代价是该入口的用户固定走这一个节点，不参与调度。
        </div>
        <div id="ee-list" data-revision="${esc(ig.external_entries_revision)}">${entryRows(ig.external_entries || [])}</div>
        <div class="form-row" style="margin-top:12px">
          <label for="ee-id">新增入口</label>
          <input id="ee-id" placeholder="入口 ID，如 friend-a" style="width:180px">
          <input id="ee-origin" placeholder="https://对方域名" style="flex:1;min-width:180px">
        </div>
        <div class="form-row">
          <label for="ee-stream">推流域名</label>
          <input id="ee-stream" placeholder="https://推流域名（留空＝按路径分流，走全部节点）" style="flex:1;min-width:200px">
          <select id="ee-node" aria-label="固定节点" style="width:130px">
            <option value="">固定节点…</option>
            ${(s.nodes || []).map((n) => `<option value="${esc(n.name)}">${esc(n.name)}</option>`).join('')}
          </select>
          <button class="btn primary" id="ee-add">登记</button>
        </div>
        <div class="form-row">
          <label for="ee-server">配置类型</label>
          <select id="ee-server" style="width:120px">
            <option value="caddy">Caddy</option><option value="nginx">nginx</option>
          </select>
          <span class="muted">nginx 配置需先安装 TLS 证书、确认端口，并通过 nginx -t</span>
        </div>
        <div id="ee-out" style="margin-top:10px"></div>
      </div>`)}
    ${card('播放调度策略', '决定同一个文件由哪个推流节点承载',
      `<div class="card-body">
        <div class="form-row"><label for="dp-policy">策略</label>
          <select id="dp-policy" style="min-width:200px">
            <option value="affinity" ${d.policy === 'affinity' ? 'selected' : ''}>文件亲和（推荐）</option>
            <option value="least-load" ${d.policy === 'least-load' ? 'selected' : ''}>最低负载</option>
          </select></div>
        <div class="form-row"><label for="dp-threshold">负载阈值</label>
          <input id="dp-threshold" type="number" step="0.05" min="0.05" max="1" value="${esc(d.load_threshold)}" style="width:110px">
          <span class="muted">节点容量占用率超过此值时改派其他节点（0.8 = 80%）</span></div>
        <div class="muted" style="margin:6px 0 10px">
          文件亲和：同一个文件固定由同一节点服务，只缓存一份，回源流量不翻倍；节点故障或过载时自动顺延。
        </div>
        <div class="toolbar"><button class="btn primary" id="dp-save">保存策略</button>
          <span id="dp-result" class="muted"></span></div>
      </div>`)}
    ${card('播放分流（Emby 接管）', p.enabled ? '已启用 — 客户端播放会被 302 分发到推流节点'
      : '未启用 — 当前所有播放仍由 Emby 主机直接吐流',
      `<div class="card-body">
        <div class="muted" style="margin-bottom:12px">
          开启后客户端播放请求经面板按文件亲和分发到节点。
          <b>转码、无法识别的条目、没有能提供该文件的节点时都会自动回退由 Emby 直供</b>，不会因面板出错导致放不了。
        </div>
        <div class="form-row"><label for="pb-enabled">启用分流</label>
          <input id="pb-enabled" type="checkbox" ${p.enabled ? 'checked' : ''}>
          <span class="muted">已有 ${mapped} 个节点配置了媒体根</span></div>
        <div class="form-row"><label for="pb-direct">仅直播</label>
          <input id="pb-direct" type="checkbox" ${p.direct_only ? 'checked' : ''}>
          <span class="muted">转码流由 Emby 主机生成，节点上没有，建议保持勾选</span></div>
        <div class="muted" style="margin:0 0 10px">
          <b>路径映射已移到「节点管理」的每个节点里</b> —— 不同节点挂载的目录和网盘身份都可能不同，
          放在全局会导致一台机器上有的库能放、有的库 404。
        </div>
        <div class="toolbar">
          <input id="pb-item" aria-label="试算 Emby ItemId" placeholder="填入 Emby ItemId 试算" style="width:190px">
          <button class="btn" id="pb-preview">预览路径</button>
          <button class="btn primary" id="pb-save">保存</button>
        </div>
        <div id="pb-result" class="muted" style="margin-top:10px"></div>
      </div>`)}
    ${tableCard('推流节点', `${s.nodes.length} 个已配置 · 媒体根、缓存、签名密钥都在「节点管理」里按节点配置`,
      ['名称', '对外地址', '媒体根', '签名', '状态'],
      s.nodes.map((n) => `<tr><td>${esc(n.name)}</td><td>${esc(n.base_url)}</td>
        <td>${(n.pools || []).length ? (n.pools || []).map((x) => esc(x.emby_prefix)).join(', ')
          : '<span class="tag bad">未配置</span>'}</td>
        <td>${n.sign_secret_set ? '<span class="tag ok">已设置</span>' : '<span class="tag bad">未设置</span>'}</td>
        <td><span class="tag ${n.enabled ? 'ok' : 'idle'}">${n.enabled ? '启用' : '停用'}</span></td></tr>`).join(''))}
    ${card('Telegram 机器人', tg.bot_token_set
      ? (tg.enabled ? '已启用 · 菜单式交互' : '已配置但未启用')
      : '未配置 — 用于成员绑定与到期提醒',
      `<div class="card-body">
        <div class="muted">
          机器人的 Token、注册开关与群组要求都在「Telegram → 机器人」页配置。
          这里只显示当前状态：同一份配置放两个表单，先后保存会互相覆盖。
        </div>
        <div class="toolbar" style="margin-top:12px">
          <span class="muted">当前状态：${esc(tgStatusText(tg))}</span>
          <button class="btn" onclick="go('tgbot')">前往配置</button>
        </div>
      </div>`)}
    ${card('会员与计费', '流量采样与 Emby 策略下发',
      `<div class="card-body">
        <div class="form-row"><label for="mb-enforcement">自动下发</label>
          <input id="mb-enforcement" type="checkbox" ${s.membership && s.membership.enforcement_enabled ? 'checked' : ''}>
          <span class="muted">关闭时只观察，不改 Emby 账号策略</span></div>
        <div class="form-row"><label for="mb-interval">采样间隔</label>
          <input id="mb-interval" type="number" min="5" max="60" value="${esc((s.membership || {}).sample_interval_seconds || 15)}" style="width:90px">
          <span class="muted">秒（5–60）</span></div>
        <div class="form-row"><label for="mb-keep">保留天数</label>
          <input id="mb-keep" type="number" min="30" value="${esc((s.membership || {}).retention_days || 400)}" style="width:90px">
          <span class="muted">播放记录与审计</span></div>
        <div class="toolbar"><button class="btn primary" id="mb-save">保存会员设置</button></div>
      </div>`)}
    ${card('图片缓存', '海报走本地磁盘，减轻 Emby CPU',
      `<div class="card-body">
        <div id="ic-stats" class="muted">读取中…</div>
        <div class="form-row"><label for="ic-enabled">启用</label>
          <input id="ic-enabled" type="checkbox" ${(s.image_cache || {}).enabled ? 'checked' : ''}></div>
        <div class="form-row"><label for="ic-gib">容量</label>
          <input id="ic-gib" type="number" min="1" value="${esc((s.image_cache || {}).max_gib || 4)}" style="width:90px">
          <span class="muted">GiB</span></div>
        <div class="form-row"><label for="ic-age">保留</label>
          <input id="ic-age" type="number" min="1" value="${esc((s.image_cache || {}).max_age_days || 30)}" style="width:90px">
          <span class="muted">天</span></div>
        <div class="toolbar">
          <button class="btn primary" id="ic-save">保存缓存设置</button>
          <button class="btn" id="ic-sweep">立即清理</button>
          <button class="btn danger" id="ic-clear">清空缓存</button>
        </div>
      </div>`)}`;
  $('#em-save').onclick = saveEmby;
  bindAsyncButton('em-test', testEmby);
  $('#dp-save').onclick = saveDispatch;
  $('#pb-save').onclick = savePlayback;
  bindAsyncButton('pb-preview', previewPlayback);
  $('#ig-save').onclick = saveIntegration;
  bindAsyncButton('ig-show', showFrontendConfig);
  $('#ee-add').onclick = addEntry;
  $('#ee-list').onclick = (ev) => {
    const btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    const action = { export: exportEntry, rotate: rotateEntry, del: removeEntry }[btn.dataset.act];
    if (action) action(btn.dataset.id);
  };
  $('#mb-save').onclick = saveMembership;
  $('#ic-save').onclick = saveImageCache;
  bindAsyncButton('ic-sweep', sweepImageCache);
  bindAsyncButton('ic-clear', clearImageCache);
  initSystemSettings();
  refreshImageCacheStats();
};
async function saveIntegration() {
  const actionContext = pageContext('settings');
  try {
    /* An empty box means "leave the stored key alone", not "delete it":
       the operator edits a URL far more often than the credential. */
    const typed = $('#ig-tmdb').value.trim();
    await api('/api/settings/integration', { method: 'PUT', body: JSON.stringify({
      panel_public_url: $('#ig-panel').value.trim(),
      emby_public_url: $('#ig-emby').value.trim(),
      tmdb_api_key: typed === '' ? SECRET_KEEP : typed,
      tmdb_language: $('#ig-tmdb-lang').value.trim() || 'zh-CN',
    }) });
    toast('接入配置已保存'); renderPage('settings', false, false, actionContext);
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
/* Registered reverse-proxy entries -------------------------------------- */
function entryRows(entries) {
  if (!entries.length) {
    return '<div class="muted">还没有登记入口。留空即可，现有播放不受影响。</div>';
  }
  return entries.map((e) => `
    <div class="form-row">
      <label style="min-width:150px"><code>${esc(e.id)}</code></label>
      <span class="muted" style="flex:1;min-width:160px">${esc(e.origin)}</span>
      <span class="muted" style="min-width:150px">${e.stream_origin
        ? '推流 ' + esc(e.stream_origin) + ' → ' + esc(e.node)
        : '按路径分流（全部节点）'}</span>
      <span class="tag ${e.proxy_key_set ? 'ok' : 'bad'}">${e.proxy_key_set ? '凭据已生成' : '缺凭据'}</span>
      <button class="btn" data-act="export" data-id="${esc(e.id)}">生成配置</button>
      <button class="btn" data-act="rotate" data-id="${esc(e.id)}">换凭据</button>
      <button class="btn danger" data-act="del" data-id="${esc(e.id)}">删除</button>
    </div>`).join('');
}
let entryBusy = false;
let entryExportGeneration = 0;
/* A fresh read alone still races another writer. The server atomically checks
   this revision, including rotations, before replacing the list. */
async function entryList() {
  const s = await api('/api/settings/integration');
  if (s.external_entries_revision !== $('#ee-list').dataset.revision) {
    throw new Error('入口已被其他页面修改，请刷新后重新操作');
  }
  return s;
}
async function writeEntries(entries, revision) {
  return api('/api/settings/integration', { method: 'PUT',
    body: JSON.stringify({ external_entries: entries, external_entries_revision: revision }) });
}
async function changeEntries(change, success) {
  if (entryBusy) return;
  entryBusy = true;
  entryExportGeneration++;
  $('#ee-out').textContent = '';
  const buttons = [...document.querySelectorAll('#ee-list button, #ee-add')];
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const s = await entryList();
    const fresh = await writeEntries(change(s.external_entries || []), s.external_entries_revision);
    toast(success);
    if ($('#ee-list')) {
      $('#ee-list').innerHTML = entryRows(fresh.external_entries || []);
      $('#ee-list').dataset.revision = fresh.external_entries_revision;
      for (const id of ['ee-id','ee-origin','ee-stream','ee-node']) { const el = $('#'+id); el.value = ''; configBaselines.set(el, ''); }
      updateDirtyBadges();
    }
  } catch (e) {
    toast('保存失败: ' + e.message, 1);
  } finally {
    entryBusy = false;
    buttons.forEach((button) => { button.disabled = false; });
  }
}
async function addEntry() {
  const id = $('#ee-id').value.trim();
  const origin = $('#ee-origin').value.trim();
  const streamOrigin = $('#ee-stream').value.trim();
  const node = $('#ee-node').value;
  if (!id || !origin) { toast('请填写入口 ID 和域名', 1); return; }
  /* Pinned mode needs both halves: a stream domain proxies exactly one node,
     so neither field alone describes a usable route. */
  if (Boolean(streamOrigin) !== Boolean(node)) {
    toast('推流域名和固定节点必须同时填写，或都留空', 1);
    return;
  }
  const row = { id, origin };
  if (streamOrigin) { row.stream_origin = streamOrigin; row.node = node; }
  await changeEntries((entries) => entries.concat([row]), '入口已登记，可以生成配置了');
}
async function removeEntry(id) {
  if (!confirm(`删除入口 ${id}？该域名的播放将不再留在它自己的域名上。`)) return;
  await changeEntries((entries) => entries.filter((e) => e.id !== id), '入口已删除');
}
async function rotateEntry(id) {
  if (!confirm(`给 ${id} 换新凭据？对方手上的旧配置会立刻失效，必须重新发送。`)) return;
  await changeEntries((entries) => {
    if (!entries.some((e) => e.id === id)) throw new Error('入口已删除，请刷新页面');
    return entries.map((e) => ({ ...e, rotate_proxy_key: e.id === id }));
  }, '凭据已轮换，请重新生成并发送配置');
}
async function exportEntry(id) {
  if (entryBusy) return;
  const generation = ++entryExportGeneration;
  const el = $('#ee-out');
  const server = $('#ee-server').value;
  el.textContent = '生成中…';
  try {
    const r = await api('/api/integration/frontend?server='
      + encodeURIComponent(server) + '&entry=' + encodeURIComponent(id));
    if (generation !== entryExportGeneration || !el.isConnected) return;
    el.innerHTML = `<div class="muted" style="margin-bottom:6px">
        <b>${esc(id)}</b> 的 ${esc(server === 'nginx' ? 'nginx' : 'Caddy')} 配置 ——
        含该入口专属凭据，<b>只发给这个入口的所有者</b>，不要公开粘贴。
      </div>
      <textarea class="codeblock" id="ee-config" readonly aria-label="入口配置" style="width:100%;height:320px"></textarea>
      <div class="toolbar" style="margin-top:6px">
        <button class="btn" id="ee-copy">复制配置</button>
        <span class="muted" id="ee-copy-hint"></span>
      </div>`;
    const config = $('#ee-config');
    config.value = r.config;
    $('#ee-copy').onclick = async () => {
      try {
        await navigator.clipboard.writeText(r.config);
        toast('已复制到剪贴板');
      } catch (err) {
        config.focus();
        config.select();
        let copied = false;
        try { copied = document.execCommand('copy'); } catch (e) { /* manual copy below */ }
        $('#ee-copy-hint').textContent = copied ? '已复制到剪贴板'
          : '配置已全选，请按 Ctrl+C / Cmd+C 复制';
      }
    };
  } catch (e) {
    if (generation === entryExportGeneration && el.isConnected) {
      el.textContent = '生成失败: ' + e.message;
    }
  }
}
async function showFrontendConfig() {
  const el = $('#ig-out');
  el.textContent = '生成中…';
  try {
    const r = await api(`/api/integration/frontend?server=${encodeURIComponent($('#ig-server').value)}`);
    el.innerHTML = `<div class="muted" style="margin-bottom:6px">把下面配置加到你的反代，然后 reload：</div>
      <pre class="codeblock">${esc(r.config)}</pre>`;
  } catch (e) { el.innerHTML = `<span class="tag bad">生成失败</span> ${esc(e.message)}`; }
}
function playbackPayload() {
  return {
    enabled: $('#pb-enabled').checked,
    direct_only: $('#pb-direct').checked,
  };
}
async function savePlayback() {
  const actionContext = pageContext('settings');
  try {
    await api('/api/settings/playback', { method: 'PUT', body: JSON.stringify(playbackPayload()) });
    toast('播放分流配置已保存'); renderPage('settings', false, false, actionContext);
  } catch (err) { toast('保存失败: ' + err.message, 1); }
}
async function previewPlayback() {
  const id = $('#pb-item').value.trim();
  const el = $('#pb-result');
  if (!id) { el.innerHTML = '<span class="tag warn">请先填入 ItemId</span>'; return; }
  el.textContent = '正在试算…';
  try {
    const r = await api(`/api/playback/preview?item_id=${encodeURIComponent(id)}`);
    el.innerHTML = r.redirected
      ? `<span class="tag ok">分流到 ${esc(r.node)} · ${esc(r.pool)}</span>
         ${r.signed ? '<span class="tag ok">已签名</span>' : '<span class="tag bad">未签名</span>'}<br>
         <span class="muted">Emby 路径</span> <code>${esc(r.media_path)}</code><br>
         <span class="muted">节点 URL</span> <code>${esc(r.target)}</code>`
      : `<span class="tag warn">不分流（${esc(r.reason)}）</span><br>
         <span class="muted">将由 Emby 直接提供</span> <code>${esc(r.target)}</code>`;
  } catch (err) {
    el.innerHTML = `<span class="tag bad">试算失败</span> ${esc(err.message)}`;
  }
}
function embyPayload() {
  const key = $('#em-key').value;
  return {
    url: $('#em-url').value.trim(),
    api_key: key === '' ? SECRET_KEEP : key,
    timeout_seconds: parseFloat($('#em-timeout').value) || 15,
    enabled: $('#em-enabled').checked,
    verify_ssl: $('#em-verify').checked,
  };
}
function tgStatusText(tg) {
  if (!tg || !tg.bot_token_set) return '未配置';
  const st = tg.status || {};
  if (!tg.enabled) return '已配置，未启用';
  if (st.last_error) return `运行中 · 最近错误：${esc(st.last_error)}`;
  return st.running ? '运行中' : '已启用，正在连接…';
}
async function saveEmby() {
  const actionContext = pageContext('settings');
  try {
    await api('/api/settings/emby', { method: 'PUT', body: JSON.stringify(embyPayload()) });
    toast('Emby 配置已保存，立即生效'); renderPage('settings', false, false, actionContext);
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
async function testEmby() {
  const el = $('#em-result');
  el.textContent = '正在测试…';
  try {
    const r = await api('/api/settings/emby/test', {
      method: 'POST', body: JSON.stringify(embyPayload()) });
    el.innerHTML = `<span class="tag ok">连接成功</span> ${esc(r.server_name || '')} ${esc(r.version || '')}`;
  } catch (e) {
    el.innerHTML = `<span class="tag bad">连接失败</span> ${esc(e.message)}`;
  }
}
async function saveDispatch() {
  try {
    await api('/api/settings/dispatch', { method: 'PUT', body: JSON.stringify({
      policy: $('#dp-policy').value,
      load_threshold: parseFloat($('#dp-threshold').value) || 0.8,
    }) });
    $('#dp-result').innerHTML = '<span class="tag ok">已保存</span>';
    toast('调度策略已生效');
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}

async function saveMembership() {
  const actionContext = pageContext('settings');
  try {
    await api('/api/settings/membership', { method: 'PUT', body: JSON.stringify({
      enforcement_enabled: $('#mb-enforcement').checked,
      sample_interval_seconds: parseInt($('#mb-interval').value, 10),
      retention_days: parseInt($('#mb-keep').value, 10),
    }) });
    toast('会员设置已保存'); renderPage('settings', false, false, actionContext);
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
async function refreshImageCacheStats() {
  const el = $('#ic-stats');
  if (!el) return;
  try {
    const r = await api('/api/settings/image-cache');
    const s = r.stats || {};
    el.innerHTML = `占用 ${fmtBytes(s.bytes)} / ${fmtBytes(s.max_bytes)} · 命中率 ${s.hit_rate == null ? '-' : s.hit_rate + '%'} · ${esc(s.entries || 0)} 张`;
  } catch (e) { el.textContent = '无法读取缓存状态'; }
}
async function saveImageCache() {
  try {
    await api('/api/settings/image-cache', { method: 'PUT', body: JSON.stringify({
      enabled: $('#ic-enabled').checked,
      max_gib: parseInt($('#ic-gib').value, 10),
      max_age_days: parseInt($('#ic-age').value, 10),
    }) });
    toast('图片缓存已保存'); refreshImageCacheStats();
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
async function sweepImageCache() {
  try { await api('/api/settings/image-cache/sweep', { method: 'POST' }); toast('已清理超限条目'); refreshImageCacheStats(); }
  catch (e) { toast('失败: ' + e.message, 1); }
}
async function clearImageCache() {
  if (!confirm('清空全部海报缓存？下次打开媒体库会重新拉取。')) return;
  try { await api('/api/settings/image-cache/clear', { method: 'POST' }); toast('已清空'); refreshImageCacheStats(); }
  catch (e) { toast('失败: ' + e.message, 1); }
}

PAGES.update = async (context = pageContext('update')) => {
  $('#view').innerHTML = pageLoading();
  const v = await api('/api/update/version');
  if (!context.isCurrent()) return;
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('⟳', esc(v.version), '当前版本', 'commit ' + esc(v.commit))}
      ${stat('☁', '<span id="latest">—</span>', '最新版本', '来自代码仓库')}
    </div>
    ${card('版本更新', '更新时服务会自动重启，页面稍后自动刷新',
      `<div class="card-body"><div class="toolbar">
        <button class="btn" id="chk">检查更新</button>
        <button class="btn primary" id="apl" disabled>一键更新</button>
        <span id="upd-flag" class="muted">尚未检查</span>
      </div></div>`)}`;
  bindAsyncButton('chk', checkUpdate);
  bindAsyncButton('apl', applyUpdate);
  checkUpdate();
};
async function checkUpdate() {
  const context = pageContext('update');
  try {
    const c = await api('/api/update/check');
    if (!context.isCurrent()) return;
    $('#latest').textContent = c.latest || '-';
    $('#upd-flag').innerHTML = c.update_available
      ? '<span class="tag warn">有新版本可用</span>' : '<span class="tag ok">已是最新</span>';
    $('#apl').disabled = !c.update_available;
  } catch (e) { toast('检查失败: ' + e.message, 1); }
}
async function applyUpdate() {
  if (!confirm('确认更新？服务将自动重启。')) return;
  try {
    const r = await api('/api/update/apply', { method: 'POST', body: JSON.stringify({}) });
    toast(`正在更新到 ${r.target}，请稍候…`);
    setTimeout(() => location.reload(), 16000);
  } catch (e) { toast('更新失败: ' + e.message, 1); }
}

/* ---------------- live updates ---------------- */
/* A push updates a cached model and patches the existing DOM. It never calls
   the navigation loader, clears #view, or refetches unrelated REST data. */
const PAGE_MODELS = Object.create(null);
const LIVE = Object.create(null);
const LIVE_UPDATERS = new Map();
const live = {src: null, data: {}, page: null, retry: 0, retryTimer: null,
  flushTimer: null, pending: new Map(), flushing: false};

function pageContext(page = state.page, liveUpdate = false) {
  const version = state.renderVersion || 0;
  const route = state.route || page;
  return {page, route, live: liveUpdate,
    isCurrent: () => state.page === page && (state.renderVersion || 0) === version
      && (state.route || state.page) === route};
}

function registerLiveUpdater(page, topics, handler) {
  LIVE[page] = [...new Set([...(LIVE[page] || []), ...topics])];
  if (!LIVE_UPDATERS.has(page)) LIVE_UPDATERS.set(page, new Set());
  LIVE_UPDATERS.get(page).add(handler);
  return () => LIVE_UPDATERS.get(page)?.delete(handler);
}

function liveKey(node) {
  if (node.nodeType !== Node.ELEMENT_NODE) return null;
  if (node.id) return 'id:' + node.id;
  if (node.dataset.liveKey) return 'key:' + node.dataset.liveKey;
  return null;
}

function patchChildren(parent, incoming) {
  const keyed = new Map([...parent.childNodes].map((node) => [liveKey(node), node])
    .filter(([key]) => key !== null));
  let cursor = parent.firstChild;
  const keep = new Set();
  for (const fresh of [...incoming.childNodes]) {
    const key = liveKey(fresh);
    let current = key ? keyed.get(key) : cursor;
    if (!key && current && liveKey(current)) current = null;
    if (current && (current.nodeType !== fresh.nodeType || current.nodeName !== fresh.nodeName)) current = null;
    if (!current) {
      current = fresh.cloneNode(true);
      parent.insertBefore(current, cursor);
    } else {
      if (current !== cursor) parent.insertBefore(current, cursor);
      patchNode(current, fresh);
    }
    keep.add(current);
    cursor = current.nextSibling;
  }
  for (const old of [...parent.childNodes]) {
    if (!keep.has(old) && !old.contains(document.activeElement)) old.remove();
  }
}

function patchNode(current, fresh) {
  if (current.isEqualNode(fresh)) return;
  if (current.nodeType !== Node.ELEMENT_NODE) {
    if (current.nodeValue !== fresh.nodeValue) current.nodeValue = fresh.nodeValue;
    return;
  }
  // These islands belong to an editor / asynchronous mount selector, not SSE.
  if (current.hasAttribute('data-live-preserve')) return;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(current.tagName)) return;
  if (current.tagName === 'BUTTON' && current.disabled) return;
  const preserve = current.tagName === 'DETAILS' ? new Set(['open']) : new Set();
  for (const attr of [...current.attributes]) {
    if (!fresh.hasAttribute(attr.name) && !preserve.has(attr.name)) current.removeAttribute(attr.name);
  }
  for (const attr of [...fresh.attributes]) {
    if (!preserve.has(attr.name) && current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name, attr.value);
  }
  patchChildren(current, fresh);
}

function renderView(html, context) {
  if (!context.isCurrent()) return false;
  const view = $('#view');
  if (!context.live) { view.innerHTML = html; return true; }
  const template = document.createElement('template');
  template.innerHTML = html;
  const focused = document.activeElement;
  const selection = focused && /^(INPUT|TEXTAREA)$/.test(focused.tagName)
    ? [focused.selectionStart, focused.selectionEnd, focused.selectionDirection] : null;
  const scrollers = [document.scrollingElement, view, ...view.querySelectorAll('*')]
    .filter((el) => el && (el.scrollTop || el.scrollLeft))
    .map((el) => [el, el.scrollTop, el.scrollLeft]);
  patchChildren(view, template.content);
  if (focused && focused.isConnected && document.activeElement !== focused) focused.focus({preventScroll:true});
  if (selection && focused.isConnected && selection[0] !== null) {
    try { focused.setSelectionRange(...selection); } catch (e) { /* number input */ }
  }
  for (const [el, top, left] of scrollers) if (el.isConnected) { el.scrollTop = top; el.scrollLeft = left; }
  return true;
}

function setLiveState(ok, label) {
  const el = $('#live-state');
  if (!el) return;
  el.className = 'tag ' + (ok ? 'ok' : 'idle');
  el.textContent = label || (ok ? '实时' : '重连中');
}

function scheduleLiveFlush() {
  if (live.flushTimer || live.flushing || !state.pageReady || !live.pending.size) return;
  live.flushTimer = setTimeout(flushLive, 24);
}

async function flushLive() {
  live.flushTimer = null;
  if (!state.pageReady || live.flushing) return;
  const context = pageContext(state.page, true);
  const pending = [...live.pending];
  live.pending.clear();
  live.flushing = true;
  try {
    for (const [topic, payload] of pending) {
      if (!context.isCurrent()) break;
      for (const handler of LIVE_UPDATERS.get(context.page) || []) {
        if (!context.isCurrent()) break;
        await handler(topic, payload, context);
      }
    }
    if (context.isCurrent()) $('#last-updated').textContent = '最近更新: ' + new Date().toLocaleTimeString();
  } catch (error) {
    if (context.isCurrent()) setLiveState(false, '更新失败 · 等待下一次推送');
  } finally {
    live.flushing = false;
    scheduleLiveFlush();
  }
}

function connectLive(page) {
  clearTimeout(live.retryTimer); live.retryTimer = null;
  clearTimeout(live.flushTimer); live.flushTimer = null;
  if (live.src) { live.src.close(); live.src = null; }
  live.pending.clear();
  if (live.page !== page) live.data = {};
  live.page = page;
  const topics = LIVE[page];
  if (!topics || !topics.length) { setLiveState(false, '按需更新'); return; }
  const src = new EventSource(`/api/stream?topics=${topics.join(',')}`);
  live.src = src;
  // Same-page detail/tab URL changes do not replace this connection. A full
  // navigation creates a different src, so obsolete callbacks still fail.
  const current = () => live.src === src && state.page === page;
  src.onopen = () => { if (current()) { live.retry = 0; setLiveState(true); } };
  topics.forEach((topic) => src.addEventListener(topic, (event) => {
    if (!current()) return;
    let payload;
    try { payload = JSON.parse(event.data); } catch (error) { return; }
    live.data[topic] = payload;
    live.pending.set(topic, payload);
    setLiveState(true);
    scheduleLiveFlush();
  }));
  src.onerror = () => {
    if (!current()) return;
    setLiveState(false);
    src.close(); live.src = null;
    live.retry = Math.min(live.retry + 1, 6);
    live.retryTimer = setTimeout(() => {
      live.retryTimer = null;
      if (live.page === page && state.page === page) connectLive(page);
    }, Math.min(30000, 1000 * 2 ** (live.retry - 1)));
  };
}

function isEditing() {
  return !!document.activeElement && /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
}

registerLiveUpdater('dashboard', ['nodes','sessions','pipeline','overview'], (topic, payload, context) => {
  const model = PAGE_MODELS.dashboard;
  if (!model) return;
  const key = {nodes:'nodes',sessions:'sessions',pipeline:'pipe',overview:'overview',latest:'latest'}[topic];
  if (key) { model[key] = payload; paintDashboard(model, context); }
});
registerLiveUpdater('nodes', ['nodes','dispatch'], (topic, payload, context) => {
  const model = PAGE_MODELS.nodes;
  if (!model) return;
  if (topic === 'nodes') model.ns = payload;
  if (topic === 'dispatch') model.log = payload;
  paintNodes(model, context);
});
for (const page of ['pipeline','mounts','tasks']) {
  registerLiveUpdater(page, [page], (topic, payload, context) => {
    if (topic !== page) return;
    ({pipeline:paintPipeline,mounts:paintMounts,tasks:paintTasks})[page](payload, context);
  });
}

/* ---------------- boot ---------------- */
/* ops.js registers PAGES.members/groups/stats/storage/audit and then
   calls bootPanel(). Starting here would paint those NAV entries as 页面不存在. */
function bootPanel() {
  if (bootPanel.done) return;
  bootPanel.done = true;
  buildNav();
  api('/api/update/version').then((v) => { $('#version').textContent = v.version; }).catch(() => {});
  api('/api/whoami').then((w) => {
    $('#who').textContent = w.user;
    $('#who-initial').textContent = (w.user || '?').slice(0, 1).toUpperCase();
  }).catch(() => {});
  go(routeFromHash());
}
