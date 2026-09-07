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
  { group: '概览', items: [
    { id: 'dashboard', icon: '▦', label: '仪表盘', sub: '集中查看系统运行、播放使用和待处理事项' },
    { id: 'stats', icon: '📈', label: '运营统计', sub: '流量、时长与热门内容' },
  ]},
  { group: '成员', items: [
    { id: 'members', icon: '☺', label: '用户管理', sub: '账号、套餐、邀请关系与积分' },
    { id: 'groups', icon: '▣', label: '套餐与用户组', sub: '时长、流量、并发与求片次数' },
    { id: 'invites', icon: '🎫', label: '邀请与授权', sub: '预授权名单、邀请名额与邀请树' },
    { id: 'redeem', icon: '🎟', label: '卡密管理', sub: '生成、发放与作废注册卡密' },
    { id: 'tgrequests', icon: '⇋', label: '关联审批', sub: '认领旧账号与换绑申请' },
  ]},
  { group: '内容', items: [
    { id: 'library', icon: '▤', label: '媒体库', sub: '媒体库分布与条目统计' },
    { id: 'requests', icon: '🎬', label: '求片', sub: '成员求片、上片员接单与处理结果' },
    { id: 'imports', icon: '⇪', label: '网盘上片', sub: '网盘链接与云盘目录导入' },
  ]},
  { group: '互动', items: [
    { id: 'tgbot', icon: '✈', label: '机器人', sub: '注册通道、名额与运行状态' },
    { id: 'shop', icon: '🎁', label: '兑换商城', sub: '积分商品、限购与兑换记录' },
    { id: 'tggroup', icon: '⚑', label: '群组核查', sub: '已关联成员的群成员状态' },
  ]},
  { group: '自动化', items: [
    { id: 'automation', icon: '⚡', label: '任务中心', sub: '任务与玩法插件的开关、配置与运行结果' },
    { id: 'tasks', icon: '⏱', label: '调度中心', sub: '主机定时任务运行状态与失败追踪' },
  ]},
  { group: '资源', items: [
    { id: 'intake', icon: '⇉', label: '入库流水线', sub: '一屏看完扫描、刷新、通知、上传与拉取' },
    { id: 'nodes', icon: '⛁', label: '节点管理', sub: '推流节点负载与调度' },
    { id: 'nodepool', icon: '⚖', label: '节点池', sub: '启停、权重、带宽与实时负载' },
    { id: 'pipeline', icon: '⇄', label: '管线状态', sub: '整理、上传队列与配额' },
    { id: 'storage', icon: '☁', label: '存储管理', sub: '云盘账号与挂载点' },
    { id: 'mounts', icon: '⛃', label: '挂载管理', sub: '存储挂载健康与缓存占用' },
  ]},
  { group: '安全', items: [
    { id: 'access', icon: '🛡', label: '访问拦截', sub: '客户端与网段规则，以及被拒记录' },
    { id: 'sharing', icon: '👥', label: '共享检测', sub: '同时多地播放的账号，只记录不处理' },
    { id: 'audit', icon: '☰', label: '审计日志', sub: '操作记录与变更追踪' },
  ]},
  { group: '系统', items: [
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
function posterUrl(itemId, maxHeight) {
  return `/emby/Items/${encodeURIComponent(itemId)}/Images/Primary`
    + `?maxHeight=${maxHeight || 420}&quality=88`;
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
    <div class="pc-poster">
      ${s.ItemId ? `<img src="${esc(posterUrl(s.ItemId, 300))}" alt="" loading="lazy">` : ''}
      <span class="pc-live">${s.Paused ? '❚❚ 已暂停' : '● 播放中'}</span>
    </div>
    <div class="pc-bd">
      <div class="pc-title">${esc(s.SeriesName ? `${s.SeriesName} · ${s.Item}` : (s.Item || '-'))}${s.ProductionYear ? `（${esc(s.ProductionYear)}）` : ''}</div>
      <div class="pc-meta">${esc(meta || '—')}</div>
      <p class="pc-ov">${esc(s.Overview || '暂无简介')}</p>
      <div class="pc-user">
        <div class="avatar">${esc((s.UserName || '?').slice(0, 1).toUpperCase())}</div>
        <div><b>${esc(s.UserName || '-')}</b><span>${esc(s.Client || '-')} · ${sessionSpeedCell(s)}</span></div>
      </div>
      ${known ? `<div class="bar wide"><i style="width:${Math.min(100, pct)}%"></i></div>
      <div class="pc-time"><span>${esc(ticksToClock(s.PositionTicks))}</span>
        <b>${esc(pct)}%</b><span>${esc(ticksToClock(s.RunTimeTicks))}</span></div>`
      : '<div class="pc-time"><span class="muted">进度不可用</span></div>'}
    </div>
  </article>`;
};

function sessionSpeedCell(s) {
  const paused = s.Paused ? ' · 已暂停' : '';
  const scope = s.SpeedScope === 'user' ? ' · 账号合计' : '';
  if (s.SpeedBps == null || !Number.isFinite(Number(s.SpeedBps)) || s.SpeedSource === 'unknown') {
    return `<span class="muted" title="${esc(s.SpeedReason || '没有新鲜的实际测量')}">未测${paused}</span>`;
  }
  const value = (Number(s.SpeedBps) / 1048576).toFixed(1);
  if (s.SpeedSource === 'node') return `${esc(value)} MiB/s${scope}${paused}`;
  return `<span title="按码率或已结束请求估算，不是实时实测">≈ ${esc(value)} MiB/s${scope}${paused}</span>`;
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
function closeModal() {
  const el = $('#modal-root');
  if (el) el.remove();
  if (_modalKey) { document.removeEventListener('keydown', _modalKey); _modalKey = null; }
}
function openModal(title, bodyHtml, opts) {
  closeModal();
  const wide = opts && opts.wide;
  const drawer = opts && opts.drawer;
  const root = document.createElement('div');
  root.id = 'modal-root';
  root.className = 'modal-root';
  root.innerHTML = `<div class="modal ${wide ? 'wide' : ''} ${drawer ? 'drawer' : ''}" role="dialog" tabindex="-1">
    <div class="modal-head"><h3>${esc(title)}</h3>
      <button class="btn sm" type="button" id="modal-close">关闭</button></div>
    <div class="modal-body">${bodyHtml}</div></div>`;
  document.body.appendChild(root);
  const box = root.querySelector('.modal');
  const focusables = () => [...box.querySelectorAll('a[href],button,input,select,textarea')]
    .filter((x) => !x.disabled && x.offsetParent !== null);
  _modalKey = (e) => {
    if (e.key === 'Escape') { closeModal(); return; }
    if (e.key !== 'Tab') return;
    const list = focusables();
    if (!list.length) return;
    const first = list[0]; const last = list[list.length - 1];
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
  $('#nav').innerHTML = NAV.map((g) => `
    <div class="nav-group">${esc(g.group)}</div>
    ${g.items.map((it) => `<div class="nav-item" data-page="${it.id}">
        <span class="ic">${it.icon}</span><span>${esc(it.label)}</span></div>`).join('')}
  `).join('');
  $('#nav').addEventListener('click', (e) => {
    const el = e.target.closest('.nav-item');
    if (el) go(el.dataset.page);
  });
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
  const page = route.split('?')[0] || 'dashboard';
  state.page = page;
  state.route = route;
  state.pageReady = false;
  stopEnrollPoll();
  const meta = navMeta(page);
  document.querySelectorAll('.nav-item').forEach((n) =>
    n.classList.toggle('active', n.dataset.page === page));
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
async function renderPage(page, manual, liveUpdate) {
  if (liveUpdate) { scheduleLiveFlush(); return; }
  const fn = PAGES[page];
  if (!fn) { $('#view').innerHTML = '<div class="empty">页面不存在</div>'; return; }
  state.renderVersion = (state.renderVersion || 0) + 1;
  state.pageReady = false;
  const context = pageContext(page);
  try {
    await fn(context);
    if (!context.isCurrent()) return;
    state.pageReady = true;
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
  const [sessions, pipe, nodes, libs, overview, latest] = await Promise.all([
    api('/api/emby/sessions').catch(() => []),
    api('/api/pipeline').catch(() => ({ available: false })),
    api('/api/nodes').catch(() => []),
    api('/api/emby/libraries').catch(() => []),
    api('/api/stats/overview?days=30').catch(() => null),
    api('/api/emby/latest?limit=12').catch(() => []),
  ]);
  if (!context.isCurrent()) return;
  PAGE_MODELS.dashboard = {sessions, pipe, nodes, libs, overview, latest};
  paintDashboard(PAGE_MODELS.dashboard, context);
};

function paintDashboard(model, context) {
  if (!context.isCurrent()) return;
  const {sessions, pipe, nodes, libs, overview, latest} = model;
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
    <div class="grid-2">
      ${card('最新入库', latest.length ? `最近 ${latest.length} 部` : '暂无数据',
        latest.length
          ? `<div class="poster-grid">${latest.map(posterTile).join('')}</div>`
          : '<div class="empty">暂无最近入库</div>')}
      ${card('正在播放', sessions.length ? `${sessions.length} 个会话` : '暂无活跃会话',
        sessions.length
          ? `<div class="card-body flush">${sessions.map(playRow).join('')}</div>`
          : '<div class="empty">当前没有播放会话</div>')}
    </div>
    ${sessions.length ? card('播放详情', '海报 · 观众 · 进度',
      `<div class="play-grid">${sessions.map(playCard).join('')}</div>`) : ''}
    <div class="grid-2">
      ${tableCard('当前播放', '实时会话 · 节点实测速率', ['用户', '客户端', '方式', '实时速度'],
        sessions.map((s) => `<tr data-live-key="session:${esc(s.Id)}"><td>${esc(s.UserName)}</td><td>${esc(s.Client)}</td>
          <td>${esc(s.PlayMethod)}${s.Paused ? ' · 已暂停' : ''}</td>
          <td>${sessionSpeedCell(s)}</td></tr>`).join(''))}
      ${tableCard('管线队列', pipe.available ? `快照 ${Math.round(pipe.snapshot_age_seconds)}s 前` : '快照不可用',
        ['队列', '条目', '体积', '最老'],
        queues.map((q) => `<tr data-live-key="queue:${esc(q.name)}"><td>${esc(q.name)}</td><td>${q.items}</td>
          <td>${fmtBytes(q.bytes)}</td><td>${fmtAge(q.oldest_age_seconds)}</td></tr>`).join(''))}
    </div>
    <div class="grid-2">
      ${tableCard('上传身份配额', '限额状态', ['身份', '状态', '受限起始'],
        (d.quota || []).map((q) => `<tr data-live-key="quota:${esc(q.identity)}"><td>${esc(q.identity)}</td>
          <td><span class="tag ${q.state === 'ok' ? 'ok' : 'bad'}">${esc(q.state)}</span></td>
          <td>${esc(q.limited_since || '-')}</td></tr>`).join(''))}
      ${card('待处理事项', '系统关键状态',
        alerts.length
          ? `<div class="card-body flush">${alerts.map((a) =>
              `<div class="list-row"><div><div class="t">${esc(a.message)}</div>
               <div class="s">${esc(a.level)}</div></div>
               <span class="tag ${a.level === 'warn' ? 'warn' : 'idle'}">${esc(a.level)}</span></div>`).join('')}</div>`
          : `<div class="empty">当前没有待处理事项</div>`)}
    </div>
    <div class="grid-2">
      ${tableCard('即将到期', '7 天内 · 点用户名进入用户管理', ['用户', '用户组', '剩余'],
        expiring.map((m) => `<tr><td><a href="#/members">${esc(m.username)}</a></td>
          <td>${esc(m.group || '-')}</td><td class="${m.days_left <= 1 ? 'danger-text' : ''}">${esc(m.days_left)} 天</td></tr>`).join(''))}
      ${card('超额 / 过期', '需要处理的账号',
        (mem.exhausted || mem.expired)
          ? `<div class="card-body"><a href="#/members">超额 ${esc(mem.exhausted || 0)} · 过期 ${esc(mem.expired || 0)} · 停用 ${esc(mem.suspended || 0)}</a></div>`
          : `<div class="empty">没有超额或过期账号</div>`)}
    </div>`, context);
}

PAGES.library = async () => {
  $('#view').innerHTML = pageLoading();
  const libs = await api('/api/emby/libraries').catch(() => []);
  const total = libs.reduce((a, l) => a + (l.items || 0), 0);
  const kinds = new Set(libs.map((l) => l.type)).size;
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('▤', libs.length, '媒体库数量', '已配置的库')}
      ${stat('≡', total.toLocaleString(), '媒体条目', '电影与剧集合计')}
      ${stat('⛁', kinds, '库类型', '按内容类型划分')}
    </div>
    ${tableCard('媒体库', `${libs.length} 个库`, ['名称', '类型', '条目数', '存储位置'],
      libs.map((l) => `<tr><td>${esc(l.name)}</td>
        <td><span class="tag idle">${esc(l.type)}</span></td>
        <td>${l.items == null ? '<span class="muted">-</span>' : Number(l.items).toLocaleString()}</td>
        <td>${esc(l.locations)} 个路径</td></tr>`).join(''))}`;
};

PAGES.imports = async () => {
  $('#view').innerHTML = pageLoading();
  const js = await api('/api/imports?limit=50').catch(() => []);
  $('#view').innerHTML = `
    ${card('新建导入', '提交网盘链接或云盘目录',
      `<div class="card-body"><div class="toolbar">
        <select id="imp-kind"><option value="drive-link">网盘链接</option><option value="cloud-drive">云盘目录</option></select>
        <input id="imp-src" placeholder="链接 / 目录引用" style="flex:1;min-width:240px">
        <input id="imp-cat" placeholder="分类(可选)" style="width:120px">
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
            ? `<button class="btn sm danger" onclick="cancelImport('${esc(j.id)}')">取消</button>` : ''}</td></tr>`;
      }).join(''))}`;
  $('#imp-go').onclick = submitImport;
};
async function submitImport() {
  const src = $('#imp-src').value.trim();
  if (!src) return toast('请输入来源', 1);
  try {
    await api('/api/imports', { method: 'POST', body: JSON.stringify({
      kind: $('#imp-kind').value, source_ref: src, category: $('#imp-cat').value.trim() }) });
    toast('任务已提交'); renderPage('imports');
  } catch (e) { toast('提交失败: ' + e.message, 1); }
}
async function cancelImport(id) {
  try { await api(`/api/imports/${id}/cancel`, { method: 'POST' }); toast('已取消'); renderPage('imports'); }
  catch (e) { toast('取消失败: ' + e.message, 1); }
}

PAGES.nodes = async (context = pageContext('nodes')) => {
  renderView(pageLoading(), context);
  const [ns, log, dispatch, st] = await Promise.all([
    api('/api/nodes').catch(() => []),
    api('/api/dispatch/log?limit=20').catch(() => []),
    api('/api/settings/dispatch').catch(() => ({ policy: '-', load_threshold: 0 })),
    api('/api/settings').catch(() => ({ integration: {} })),
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
  const egress = ns.reduce((a, n) => a + (n.egress_mbps || 0), 0);
  const policyLabel = dispatch.policy === 'affinity' ? '文件亲和' : '最低负载';
  const panelSet = !!(st.integration || {}).panel_public_url;

  renderView(`
    <div class="stat-grid">
      ${stat('⛁', `${online} / ${ns.length}`, '在线节点', '可用于分发')}
      ${stat('▶', streams, '活跃流', '所有节点合计')}
      ${stat('⇅', (egress / 8).toFixed(1), '出口 MB/s', '节点实时出口')}
      ${stat('⚖', policyLabel, '调度策略', dispatch.policy === 'affinity'
        ? `占用率阈值 ${Math.round(dispatch.load_threshold * 100)}%` : '按容量占用率择优')}
    </div>
    ${panelSet ? '' : card('⚠ 尚未填写面板对外地址', '节点安装时需要用它回连面板取配置',
      `<div class="card-body"><div class="muted">请先到「系统设置 → 接入方式」填写面板对外地址，否则无法生成节点安装命令。</div></div>`)}
    ${card('新增节点', '只需名称；地址由节点安装后自动上报',
      `<div class="card-body"><div class="toolbar">
        <input id="nd-name" placeholder="节点名称 如 node-a" style="width:160px">
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
  $('#nd-go').onclick = addNode;
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
        <td><button class="btn sm danger" onclick="delPool('${esc(n.name)}',${i})">删除</button></td>
      </tr>`).join('')
    : '';
  return card(`⛁ ${n.name}`,
    `${n.active_streams}/${n.capacity} 路 · ${Math.round((n.utilisation || 0) * 100)}% · ${n.egress_mbps} Mbps`,
    `<div class="card-body">
      <div class="toolbar" style="margin-bottom:10px">
        ${health}
        <span class="muted">${esc(n.base_url)}</span>
        <span style="flex:1"></span>
        <button class="btn sm" onclick="nodeCtl('${esc(n.name)}','${n.manually_disabled ? 'enable' : 'disable'}')">${n.manually_disabled ? '上线' : '下线'}</button>
        <button class="btn sm" onclick="editNodeCapacity('${esc(n.name)}',${n.capacity})">改容量</button>
        <button class="btn sm danger" onclick="deleteNode('${esc(n.name)}')">删除</button>
      </div>

      <div class="sub" style="margin:12px 0 4px"><b>媒体根映射</b> — Emby 里的路径对应节点上的哪个目录</div>
      ${poolRows
        ? `<table><thead><tr><th>名称</th><th>Emby 路径</th><th>节点路径</th><th>rclone remote</th><th>URL 前缀</th><th></th></tr></thead><tbody>${poolRows}</tbody></table>`
        : '<div class="empty">未配置媒体根 — 该节点当前无法提供任何文件</div>'}
      <div class="toolbar" style="margin-top:8px">
        <input id="pl-name-${esc(n.name)}" placeholder="名称 main" style="width:90px">
        <input id="pl-emby-${esc(n.name)}" placeholder="Emby 路径 /media" style="width:150px">
        <input id="pl-path-${esc(n.name)}" placeholder="节点路径 /mnt/gdrive/Media" style="flex:1;min-width:170px">
        <input id="pl-remote-${esc(n.name)}" placeholder="remote rc2:Media" style="width:150px">
        <input id="pl-url-${esc(n.name)}" placeholder="URL 前缀 /s/main" style="width:120px">
        <button class="btn" onclick="addPool('${esc(n.name)}')">添加媒体根</button>
      </div>

      <div class="sub" style="margin:14px 0 4px"><b>存储与安全</b></div>
      <div class="form-row"><label>缓存目录</label>
        <input id="nc-dir-${esc(n.name)}" value="${esc(n.cache_dir || '')}" placeholder="/var/cache/mediadeck"></div>
      <div class="form-row"><label>缓存上限</label>
        <input id="nc-size-${esc(n.name)}" value="${esc(n.cache_size || '')}" placeholder="2T" style="width:120px">
        <span class="muted">别超过该盘可用空间</span></div>
      <div class="form-row"><label>签名密钥</label>
        <span class="${n.sign_secret_set ? 'tag ok' : 'tag bad'}">${n.sign_secret_set ? '已设置' : '未设置 · 链接永久公开'}</span>
        <span class="muted">${esc(n.sign_secret_masked || '')}</span>
        <button class="btn sm" onclick="rotateNodeSecret('${esc(n.name)}')">重置密钥</button></div>
      <div class="form-row"><label>签名参数</label>
        <input id="nc-argd-${esc(n.name)}" value="${esc(n.sign_arg_digest || 'md5')}" style="width:90px">
        <input id="nc-arge-${esc(n.name)}" value="${esc(n.sign_arg_expires || 'expires')}" style="width:110px">
        <span class="muted">节点 nginx 用的参数名（已有站点常用 k / e）</span></div>
      <div class="form-row"><label>链接有效期</label>
        <input id="nc-ttl-${esc(n.name)}" type="number" min="60" value="${esc(n.sign_ttl_seconds || 21600)}" style="width:120px">
        <span class="muted">秒</span></div>
      ${n.legacy_config ? `<div class="form-row"><label>旧式配置</label>
        <span class="tag warn">该节点仍保存独立 rclone.conf</span>
        <button class="btn sm" onclick="migrateNodeStorage('${esc(n.name)}')">迁移到全局挂载</button></div>` : ''}
      <div class="form-row"><label>全局挂载</label>
        <div id="nmounts-${esc(n.name)}" data-live-preserve class="muted">加载中…</div></div>
      <div class="form-row"><label>接入状态</label>
        ${n.enrolled ? `<span class="tag ok">已接入</span> <span class="muted">${esc(fmtAgeTs(n.first_seen_at))} · ${esc(n.enrolled_host || n.base_url)}</span>`
                      : '<span class="tag warn">待接入</span>'}</div>
      <div class="toolbar">
        <button class="btn primary" onclick="saveNodeStorage('${esc(n.name)}')">保存</button>
        <button class="btn" onclick="showEnroll('${esc(n.name)}')">获取安装命令</button>
      </div>
      <div id="enroll-${esc(n.name)}" data-live-preserve style="margin-top:10px"></div>
    </div>`);
}

async function addPool(name) {
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
    toast('媒体根已添加'); renderPage('nodes');
  } catch (e) { toast('添加失败: ' + e.message, 1); }
}
async function delPool(name, index) {
  const node = (state.nodes || []).find((n) => n.name === name);
  if (!node) return;
  const pools = (node.pools || []).filter((_, i) => i !== index);
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify({ pools }) });
    toast('已删除'); renderPage('nodes');
  } catch (e) { toast('删除失败: ' + e.message, 1); }
}
async function saveNodeStorage(name) {
  const g = (k) => ($(`#nc-${k}-${CSS.escape(name)}`) || {}).value || '';
  const mountIds = [...document.querySelectorAll(`.nmount-${CSS.escape(name)}:checked`)].map((x) => x.value);
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify({
      cache_dir: g('dir').trim(), cache_size: g('size').trim(),
      sign_arg_digest: g('argd').trim(), sign_arg_expires: g('arge').trim(),
      sign_ttl_seconds: parseInt(g('ttl'), 10) || 21600,
      mount_ids: mountIds,
    }) });
    toast('已保存'); renderPage('nodes');
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
async function rotateNodeSecret(name) {
  if (!confirm(`重置 ${name} 的签名密钥？\n\n已发出的播放链接会立即失效，且必须重新在节点上执行安装命令。`)) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}/rotate-secret`, { method: 'POST' });
    toast('密钥已重置，请重新部署该节点'); renderPage('nodes');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function editRcloneConf(name) {
  const text = prompt(`粘贴该节点使用的 rclone.conf 全文\n（建议为节点单独建 OAuth 身份，避免和主机抢配额）`);
  if (text === null) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify({ rclone_conf: text }) });
    toast('已保存'); renderPage('nodes');
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
function stopEnrollPoll() {
  if (state.enrollTimer) { clearInterval(state.enrollTimer); state.enrollTimer = null; }
}
async function showEnroll(name) {
  stopEnrollPoll();
  const box = $(`#enroll-${CSS.escape(name)}`);
  if (!box) return;
  box.textContent = '生成中…';
  const paint = async () => {
    try {
      const r = await api(`/api/nodes/${encodeURIComponent(name)}/enroll`);
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
      box.innerHTML = `<span class="tag bad">生成失败</span> ${esc(e.message)}`;
      stopEnrollPoll();
    }
  };
  await paint();
  state.enrollTimer = setInterval(paint, 4000);
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
  try { await api(`/api/nodes/${encodeURIComponent(name)}/${action}`, { method: 'POST' });
    toast(action === 'disable' ? '已下线' : '已上线'); renderPage('nodes');
  } catch (e) { toast('操作失败: ' + e.message, 1); }
}
async function addNode() {
  const body = {
    name: $('#nd-name').value.trim(),
    capacity: parseFloat($('#nd-capacity').value) || 100,
  };
  if (!body.name) return toast('请填写节点名称', 1);
  try {
    const created = await api('/api/nodes', { method: 'POST', body: JSON.stringify(body) });
    toast('节点已登记');
    await renderPage('nodes');
    showEnroll(created.name);
  } catch (e) { toast('添加失败: ' + e.message, 1); }
}
async function fillNodeMounts(n) {
  const box = $(`#nmounts-${CSS.escape(n.name)}`);
  if (!box) return;
  try {
    const mounts = await api('/api/storage/mounts');
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
  if (!confirm(`把 ${name} 的旧式 rclone.conf 标记为已迁移？\n\n独立配置会保留，但之后请改用全局挂载列表。`)) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify({ mount_ids: [] }) });
    toast('已切换为全局挂载模式（旧配置仍保留，不会静默删除）');
    renderPage('nodes');
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function editNodeCapacity(name, current) {
  const value = prompt(`设置 ${name} 的并发容量（最多同时承载多少路播放）`, current);
  if (value === null) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, {
      method: 'PUT', body: JSON.stringify({ capacity: parseFloat(value) }) });
    toast('容量已更新'); renderPage('nodes');
  } catch (e) { toast('更新失败: ' + e.message, 1); }
}
async function deleteNode(name) {
  if (!confirm(`确认删除节点 ${name}？该节点将不再参与播放分发。`)) return;
  try {
    await api(`/api/nodes/${encodeURIComponent(name)}`, { method: 'DELETE' });
    toast('节点已删除'); renderPage('nodes');
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

PAGES.settings = async () => {
  $('#view').innerHTML = pageLoading();
  const s = await api('/api/settings').catch(() => null);
  if (!s) { $('#view').innerHTML = `<div class="card"><div class="empty">设置加载失败</div></div>`; return; }
  const e = s.emby, d = s.dispatch, p = s.playback, ig = s.integration;
  const tg = await api('/api/settings/telegram').catch(() => ({}));
  const connected = e.enabled && e.api_key_set;
  const mapped = (s.nodes || []).filter((n) => (n.pools || []).length).length;
  $('#view').innerHTML = `
    ${s.mock_mode ? card('演示模式', '当前以 MEDIADECK_MOCK=1 运行',
      `<div class="card-body"><div class="muted">所有数据均为模拟值，保存的配置不会连接真实服务。</div></div>`) : ''}
    ${card('Emby 对接', connected ? '已连接' : '尚未连接 — 用户管理与媒体库依赖此配置',
      `<div class="card-body">
        <div class="form-row"><label>服务器地址</label>
          <input id="em-url" value="${esc(e.url)}" placeholder="http://127.0.0.1:8096"></div>
        <div class="form-row"><label>API Key</label>
          <input id="em-key" type="password" placeholder="${e.api_key_set ? esc(e.api_key_masked) + '（留空则不修改）' : '在 Emby 后台「高级 → API 密钥」创建'}"></div>
        <div class="form-row"><label>请求超时</label>
          <input id="em-timeout" type="number" min="1" max="120" value="${esc(e.timeout_seconds)}" style="width:110px"> <span class="muted">秒</span></div>
        <div class="form-row"><label>启用集成</label>
          <input id="em-enabled" type="checkbox" ${e.enabled ? 'checked' : ''}>
          <span class="muted">关闭后面板不再调用 Emby</span></div>
        <div class="form-row"><label>校验证书</label>
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
        <div class="form-row"><label>面板地址</label>
          <input id="ig-panel" value="${esc(ig.panel_public_url)}" placeholder="https://deck.example.com"></div>
        <div class="form-row"><label>Emby 地址</label>
          <input id="ig-emby" value="${esc(ig.emby_public_url)}" placeholder="https://emby.example.com"></div>
        <div class="muted" style="margin:14px 0 8px">
          <b>TMDB</b>（可选）。填了之后成员求片会显示片名、年份和海报；
          不填也能用，求片只会记下 TMDB 编号，上片员照样能处理。
          <a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noreferrer">去申请 Key</a>
        </div>
        <div class="form-row"><label>TMDB Key</label>
          <input id="ig-tmdb" type="password" autocomplete="new-password"
            placeholder="${ig.tmdb_api_key_set ? esc(ig.tmdb_api_key_masked) : '未配置'}">
          <span class="muted">${ig.tmdb_api_key_set ? '已配置，留空表示不修改' : '留空表示不启用'}</span></div>
        <div class="form-row"><label>TMDB 语言</label>
          <input id="ig-tmdb-lang" value="${esc(ig.tmdb_language || 'zh-CN')}" style="width:120px">
          <span class="muted">zh-CN / en-US / ja-JP</span></div>
        <div class="toolbar">
          <select id="ig-server" style="width:120px">
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
          <label>新增入口</label>
          <input id="ee-id" placeholder="入口 ID，如 friend-a" style="width:180px">
          <input id="ee-origin" placeholder="https://对方域名" style="flex:1;min-width:180px">
        </div>
        <div class="form-row">
          <label>推流域名</label>
          <input id="ee-stream" placeholder="https://推流域名（留空＝按路径分流，走全部节点）" style="flex:1;min-width:200px">
          <select id="ee-node" style="width:130px">
            <option value="">固定节点…</option>
            ${(s.nodes || []).map((n) => `<option value="${esc(n.name)}">${esc(n.name)}</option>`).join('')}
          </select>
          <button class="btn primary" id="ee-add">登记</button>
        </div>
        <div class="form-row">
          <label>配置类型</label>
          <select id="ee-server" style="width:120px">
            <option value="caddy">Caddy</option><option value="nginx">nginx</option>
          </select>
          <span class="muted">nginx 配置需先安装 TLS 证书、确认端口，并通过 nginx -t</span>
        </div>
        <div id="ee-out" style="margin-top:10px"></div>
      </div>`)}
    ${card('播放调度策略', '决定同一个文件由哪个推流节点承载',
      `<div class="card-body">
        <div class="form-row"><label>策略</label>
          <select id="dp-policy" style="min-width:200px">
            <option value="affinity" ${d.policy === 'affinity' ? 'selected' : ''}>文件亲和（推荐）</option>
            <option value="least-load" ${d.policy === 'least-load' ? 'selected' : ''}>最低负载</option>
          </select></div>
        <div class="form-row"><label>负载阈值</label>
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
        <div class="form-row"><label>启用分流</label>
          <input id="pb-enabled" type="checkbox" ${p.enabled ? 'checked' : ''}>
          <span class="muted">已有 ${mapped} 个节点配置了媒体根</span></div>
        <div class="form-row"><label>仅直播</label>
          <input id="pb-direct" type="checkbox" ${p.direct_only ? 'checked' : ''}>
          <span class="muted">转码流由 Emby 主机生成，节点上没有，建议保持勾选</span></div>
        <div class="muted" style="margin:0 0 10px">
          <b>路径映射已移到「节点管理」的每个节点里</b> —— 不同节点挂载的目录和网盘身份都可能不同，
          放在全局会导致一台机器上有的库能放、有的库 404。
        </div>
        <div class="toolbar">
          <input id="pb-item" placeholder="填入 Emby ItemId 试算" style="width:190px">
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
        <div class="form-row"><label>自动下发</label>
          <input id="mb-enforcement" type="checkbox" ${s.membership && s.membership.enforcement_enabled ? 'checked' : ''}>
          <span class="muted">关闭时只观察，不改 Emby 账号策略</span></div>
        <div class="form-row"><label>采样间隔</label>
          <input id="mb-interval" type="number" min="5" max="60" value="${esc((s.membership || {}).sample_interval_seconds || 15)}" style="width:90px">
          <span class="muted">秒（5–60）</span></div>
        <div class="form-row"><label>保留天数</label>
          <input id="mb-keep" type="number" min="30" value="${esc((s.membership || {}).retention_days || 400)}" style="width:90px">
          <span class="muted">播放记录与审计</span></div>
        <div class="toolbar"><button class="btn primary" id="mb-save">保存会员设置</button></div>
      </div>`)}
    ${card('图片缓存', '海报走本地磁盘，减轻 Emby CPU',
      `<div class="card-body">
        <div id="ic-stats" class="muted">读取中…</div>
        <div class="form-row"><label>启用</label>
          <input id="ic-enabled" type="checkbox" ${(s.image_cache || {}).enabled ? 'checked' : ''}></div>
        <div class="form-row"><label>容量</label>
          <input id="ic-gib" type="number" min="1" value="${esc((s.image_cache || {}).max_gib || 4)}" style="width:90px">
          <span class="muted">GiB</span></div>
        <div class="form-row"><label>保留</label>
          <input id="ic-age" type="number" min="1" value="${esc((s.image_cache || {}).max_age_days || 30)}" style="width:90px">
          <span class="muted">天</span></div>
        <div class="toolbar">
          <button class="btn primary" id="ic-save">保存缓存设置</button>
          <button class="btn" id="ic-sweep">立即清理</button>
          <button class="btn danger" id="ic-clear">清空缓存</button>
        </div>
      </div>`)}`;
  $('#em-save').onclick = saveEmby;
  $('#em-test').onclick = testEmby;
  $('#dp-save').onclick = saveDispatch;
  $('#pb-save').onclick = savePlayback;
  $('#pb-preview').onclick = previewPlayback;
  $('#ig-save').onclick = saveIntegration;
  $('#ig-show').onclick = showFrontendConfig;
  $('#ee-add').onclick = addEntry;
  $('#ee-list').onclick = (ev) => {
    const btn = ev.target.closest('button[data-act]');
    if (!btn) return;
    const action = { export: exportEntry, rotate: rotateEntry, del: removeEntry }[btn.dataset.act];
    if (action) action(btn.dataset.id);
  };
  $('#mb-save').onclick = saveMembership;
  $('#ic-save').onclick = saveImageCache;
  $('#ic-sweep').onclick = sweepImageCache;
  $('#ic-clear').onclick = clearImageCache;
  refreshImageCacheStats();
};
async function saveIntegration() {
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
    toast('接入配置已保存'); renderPage('settings');
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
  await api('/api/settings/integration', { method: 'PUT',
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
    await writeEntries(change(s.external_entries || []), s.external_entries_revision);
    toast(success);
    await renderPage('settings');
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
  try {
    await api('/api/settings/playback', { method: 'PUT', body: JSON.stringify(playbackPayload()) });
    toast('播放分流配置已保存'); renderPage('settings');
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
  try {
    await api('/api/settings/emby', { method: 'PUT', body: JSON.stringify(embyPayload()) });
    toast('Emby 配置已保存，立即生效'); renderPage('settings');
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
  try {
    await api('/api/settings/membership', { method: 'PUT', body: JSON.stringify({
      enforcement_enabled: $('#mb-enforcement').checked,
      sample_interval_seconds: parseInt($('#mb-interval').value, 10),
      retention_days: parseInt($('#mb-keep').value, 10),
    }) });
    toast('会员设置已保存'); renderPage('settings');
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

PAGES.update = async () => {
  $('#view').innerHTML = pageLoading();
  const v = await api('/api/update/version').catch(() => ({ version: '?', commit: '?' }));
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
  $('#chk').onclick = checkUpdate;
  $('#apl').onclick = applyUpdate;
  checkUpdate();
};
async function checkUpdate() {
  try {
    const c = await api('/api/update/check');
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

registerLiveUpdater('dashboard', ['nodes','sessions','pipeline','overview','latest'], (topic, payload, context) => {
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
