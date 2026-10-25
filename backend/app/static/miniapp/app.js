(() => {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const S = {
    me: null, tab: 'me', stack: [], version: 0,
    rank: { kind: 'watch', period: 'today' }, bag: 'shop',
    req: { q: '', type: '', results: null, page: 1, total: 1 },
  };
  const ORN = { 1: '⋆', 2: '✦', 3: '❀', 4: '❄', 5: '✧', 6: '◈', 7: '♛', 8: '✺' };
  const STATUS = {
    active: ['ok', '正常'], suspended: ['bad', '已停用'], expired: ['warn', '已过期'],
    exhausted: ['warn', '已超额'], pending: ['', '待开通'], deleted: ['bad', '已删除'],
  };
  const REQ_STATUS = { open: ['warn', '待处理'], accepted: ['ok', '已接受'], rejected: ['bad', '未通过'], cancelled: ['', '已取消'] };
  const KIND_ICON = {
    bandwidth_card: ['gauge', '#6aa6ff'], streams_card: ['monitor-play', '#43d39e'], title_card: ['award', '#ffb84d'],
    invite_card: ['ticket', '#c07bff'], whitelist_card: ['shield-check', '#9fe7ff'], custom: ['box', '#8b93a7'],
  };
  const emo = (s) => (s && [...s].length === 1 ? s + '\uFE0F' : s || '');
  function icon(name) {
    return `<svg class="ico" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  }

  // -- platform ---------------------------------------------------------------

  function applyTheme() {
    const dark = !tg || tg.colorScheme !== 'light';
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    const bg = dark ? '#0b0e17' : '#f4f5fb';
    try { tg && tg.setHeaderColor(bg); tg && tg.setBackgroundColor(bg); } catch (_) { /* older clients */ }
  }
  if (tg) {
    tg.ready();
    tg.expand();
    try { tg.disableVerticalSwipes(); } catch (_) { /* older clients */ }
    tg.onEvent('themeChanged', applyTheme);
    tg.BackButton.onClick(() => back());
  }
  applyTheme();

  const haptic = {
    ok() { try { tg.HapticFeedback.notificationOccurred('success'); } catch (_) { /* optional */ } },
    bad() { try { tg.HapticFeedback.notificationOccurred('error'); } catch (_) { /* optional */ } },
    tap() { try { tg.HapticFeedback.impactOccurred('light'); } catch (_) { /* optional */ } },
  };

  class ApiError extends Error {
    constructor(status, code, message) { super(message); this.status = status; this.code = code; }
  }

  async function api(path, { method = 'GET', body } = {}) {
    let res;
    try {
      res = await fetch(path, {
        method,
        headers: { 'X-Telegram-Init-Data': (tg && tg.initData) || '', ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch (_) {
      throw new ApiError(0, 'network', '网络连接失败，请检查网络后重试');
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) {
      const d = data && data.detail;
      const message = (d && typeof d === 'object' && d.message) || (typeof d === 'string' && d) || '服务暂时不可用，请稍后再试';
      throw new ApiError(res.status, (d && d.code) || '', message);
    }
    return data;
  }

  // -- formatting -------------------------------------------------------------

  const pad = (n) => String(n).padStart(2, '0');
  function bj(ts) { return new Date((Number(ts) + 8 * 3600) * 1000); }
  function stamp(ts, year = false) {
    if (!ts) return '永久';
    const d = bj(ts);
    const md = `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
    return year ? `${d.getUTCFullYear()}-${md}` : md;
  }
  function day(ts) {
    if (!ts) return '—';
    const d = bj(ts);
    return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
  }
  function todayKey() {
    const d = bj(Date.now() / 1000);
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
  }
  function left(ts) {
    if (!ts) return '永久有效';
    const s = ts - Date.now() / 1000;
    if (s <= 0) return '已到期';
    if (s >= 86400) return `剩 ${Math.floor(s / 86400)} 天`;
    return `剩不到 ${Math.max(1, Math.floor(s / 3600) + 1)} 小时`;
  }
  function dur(seconds) {
    const s = Math.max(0, Math.round(Number(seconds) || 0));
    if (s < 60) return s ? '不到 1 分钟' : '0 分钟';
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return h ? `${h}小时${pad(m)}分` : `${m}分钟`;
  }
  const num = (n) => Number(n || 0).toLocaleString('zh-CN');
  function initial(name) {
    const text = String(name || '').replace(/^@/, '').trim();
    const ch = Array.from(text)[0] || '?';
    return esc(ch.toUpperCase());
  }
  function face(name, url, cls = '', style = '') {
    const img = url ? `<img src="${esc(url)}" alt="" loading="lazy" onerror="this.remove()">` : '';
    return `<div class="avatar${cls ? ' ' + cls : ''}"${style ? ` style="${style}"` : ''}>${initial(name)}${img}</div>`;
  }
  function ago(ts) {
    if (!ts) return '—';
    const s = Date.now() / 1000 - ts;
    if (s < 60) return '刚刚';
    if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
    const d = bj(ts);
    const now = bj(Date.now() / 1000);
    const hm = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
    const days = Math.floor((Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())
      - Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate())) / 86400000);
    if (days === 0) return `今天 ${hm}`;
    if (days === 1) return `昨天 ${hm}`;
    if (days < 30) return `${days} 天前`;
    return day(ts);
  }
  function hue(text) {
    let h = 0;
    for (const ch of String(text)) h = (h * 31 + ch.codePointAt(0)) % 360;
    return h;
  }
  const mbps = (kbps) => (kbps ? Math.round(kbps / 1000) : null);

  function lvClass(tag) {
    if (!tag) return '';
    return tag.whitelist ? 'wl' : `lv${tag.level}`;
  }
  function lvTag(tag, xs = false, bare = false) {
    if (!tag) return '';
    const label = tag.whitelist ? '💠 白名单'
      : `${bare || !tag.icon ? '' : esc(emo(tag.icon)) + ' '}Lv${esc(tag.level)}${xs ? '' : ' ' + esc(tag.name || '')}`;
    return `<span class="lvtag ${xs ? 'xs ' : ''}${lvClass(tag)}">${label}</span>`;
  }
  function growthMeter(lv, nextLevel, today) {
    // Same reading as the Bot's "成长 120 / 360": share of the next threshold reached.
    const p = lv.next_threshold ? Math.max(0, Math.min(100, Math.floor((lv.growth || 0) / lv.next_threshold * 100))) : 100;
    const g = num(lv.growth || 0);
    const td = today && today.growth > 0 ? `<em class="gm-today num"> · 今日 +${num(today.growth)}</em>` : '';
    const next = lv.next_threshold;
    if (!next) {
      const isEx = lv.level === 8;
      return `<div class="gm">
        <div class="gm-top"><span class="gm-label"><b>${lv.icon ? esc(emo(lv.icon)) + ' ' : ''}Lv${esc(lv.level)} ${esc(lv.name || '')}</b> · ${isEx ? '专属授予等级' : '已达最高常规等级'}</span><b class="gm-pct num">${isEx ? '专属' : '已满级'}</b></div>
        <div class="gm-bar" role="progressbar" aria-valuenow="100" aria-valuemin="0" aria-valuemax="100"><span style="width:100%"></span></div>
        <div class="gm-foot"><span>累计成长 <b class="num">${g}</b>${td}</span><span class="gm-stage num">Lv${esc(lv.level)} MAX</span></div>
      </div>`;
    }
    const gap = Math.max(0, next - (lv.growth || 0));
    const nextLv = lv.level + 1;
    const nextName = nextLevel && nextLevel.name ? ` ${esc(nextLevel.name)}` : '';
    const nextIcon = nextLevel && nextLevel.icon ? `${esc(emo(nextLevel.icon))} ` : '';
    return `<div class="gm">
      <div class="gm-top"><span class="gm-label">距 <b>${nextIcon}Lv${esc(nextLv)}${nextName}</b> 还差 <b class="num">${num(gap)}</b> 成长</span><b class="gm-pct num">${p}%</b></div>
      <div class="gm-bar" role="progressbar" aria-valuenow="${p}" aria-valuemin="0" aria-valuemax="100"><span style="width:${p}%${p > 0 ? ';min-width:4px' : ''}"></span></div>
      <div class="gm-foot"><span>成长 <b class="num">${g}</b> / <span class="num">${num(next)}</span>${td}</span><span class="gm-stage num">Lv${esc(lv.level)} → Lv${esc(nextLv)}</span></div>
    </div>`;
  }
  function plaque(text, tag, big = false, ident = null) {
    const tier = tag && tag.whitelist ? 7 : Math.min(8, Math.max(1, Number(tag && tag.level) || 1));
    const orn = `<span class="orn">${tag && tag.whitelist ? '◆' : ORN[tier]}</span>`;
    const head = ident && ident.name
      ? `<span class="txt nm">${esc(ident.name)}</span>${ident.icon ? `<span class="emb">${esc(emo(ident.icon))}</span>` : ''}` : '';
    return `<span class="plaque t${tier}${big ? ' big' : ''}">${orn}${head}<span class="txt">${esc(text)}</span>${orn}</span>`;
  }
  function myTag() {
    const me = S.me;
    if (!me) return null;
    return me.whitelist ? { whitelist: true } : { level: me.level.level, name: me.level.name, icon: me.level.icon };
  }
  function posterHtml(src, title, extra = '') {
    const img = src ? `<img src="${esc(src)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.parentElement.classList.add('no-img');this.remove()">` : '';
    return `<span class="ph">${icon('clapperboard')}</span>${img}${extra}<span class="cap">${esc(title)}</span>`;
  }

  // -- shell ------------------------------------------------------------------

  const screen = $('#screen');
  let toastTimer = 0;
  function toast(message, bad = false) {
    const el = $('#toast');
    el.textContent = message;
    el.className = bad ? 'bad' : '';
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 4200 : 2400);
    if (bad) haptic.bad();
  }

  function setScreen(html) {
    screen.innerHTML = html;
    screen.scrollTop = 0;
  }
  function loading(target = screen) { target.innerHTML = '<div class="loading"><span class="spin"></span>加载中…</div>'; }
  function stateView(glyph, title, text, action = '') {
    return `<div class="state"><div class="big">${glyph}</div><b>${esc(title)}</b><div>${esc(text)}</div>${action}</div>`;
  }
  function failView(err, retry = true) {
    return stateView(icon('circle-alert'), '加载失败', err.message || '请稍后再试',
      retry ? '<button class="btn ghost" type="button" data-retry>重试</button>' : '');
  }

  function current() { return S.stack.length ? S.stack[S.stack.length - 1] : null; }
  function syncChrome() {
    const top = current();
    $('#tabbar').hidden = !!top || !S.me;
    $$('#tabbar button').forEach((b) => b.classList.toggle('on', b.dataset.tab === S.tab));
    if (tg) { if (top) tg.BackButton.show(); else tg.BackButton.hide(); }
  }
  async function render() {
    games.dispose();
    const version = ++S.version;
    syncChrome();
    const top = current();
    const page = top ? top.render : TABS[S.tab];
    try {
      await page(() => version === S.version);
    } catch (err) {
      if (version !== S.version) return;
      if (fatal(err)) return;
      setScreen(failView(err));
    }
  }
  function push(renderFn) { S.stack.push({ render: renderFn }); render(); }
  function back() {
    if (!$('#layer').hidden) { closeLayer(); return; }
    if (S.stack.length) { S.stack.pop(); render(); }
  }
  function goTab(tab, opts = {}) {
    S.stack = [];
    S.tab = tab;
    if (opts.bag) S.bag = opts.bag;
    render();
  }
  screen.addEventListener('click', (e) => {
    if (e.target.closest('[data-retry]')) render();
  });
  $('#tabbar').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-tab]');
    if (!btn) return;
    haptic.tap();
    goTab(btn.dataset.tab);
  });

  function fatal(err) {
    if (!(err instanceof ApiError)) return false;
    if (err.status === 401 || err.status === 403 || err.status === 503) {
      const glyph = icon(err.code === 'unbound' ? 'unlink' : err.code === 'disabled' ? 'wrench' : 'lock');
      const title = err.code === 'unbound' ? '还没有绑定账号' : err.code === 'disabled' ? '会员面板暂未开放' : '无法确认身份';
      S.me = null;
      S.stack = [];
      syncChrome();
      setScreen(stateView(glyph, title, err.message,
        tg ? '<button class="btn" type="button" data-close>回到 Bot</button>' : ''));
      const close = $('[data-close]', screen);
      if (close) close.onclick = () => tg.close();
      return true;
    }
    return false;
  }

  // -- layers -----------------------------------------------------------------

  let layerResolve = null;
  function closeLayer(value = false) {
    const layer = $('#layer');
    layer.hidden = true;
    layer.innerHTML = '';
    const done = layerResolve;
    layerResolve = null;
    if (done) done(value);
  }
  function sheet(html, bind) {
    if (layerResolve) closeLayer();
    const layer = $('#layer');
    layer.innerHTML = `<div class="dim" data-dismiss></div><div class="sheet" role="dialog" aria-modal="true"><div class="grab"></div>${html}</div>`;
    layer.hidden = false;
    $('[data-dismiss]', layer).onclick = () => closeLayer(false);
    $$('[data-cancel]', layer).forEach((b) => { b.onclick = () => closeLayer(false); });
    return new Promise((resolve) => {
      layerResolve = resolve;
      if (bind) bind($('.sheet', layer), (v) => closeLayer(v));
    });
  }
  function kv(rows) {
    return rows.map(([k, v]) => `<div class="kv"><span class="muted">${esc(k)}</span><span class="num">${v}</span></div>`).join('');
  }
  /* A confirmation layer; onConfirm runs while the layer stays open so a failure
     can be shown in place and retried with the same idempotency key. */
  function confirmSheet({ title, tag = '', rows = [], note = '', okText, danger = false, input = null, onConfirm }) {
    const field = input
      ? `<input class="field" id="sheet-input" maxlength="${input.max || 40}" placeholder="${esc(input.placeholder || '')}" autocomplete="off">
         <div class="tiny muted">${esc(input.hint || '')}</div>` : '';
    return sheet(`
      <div class="row" style="margin-bottom:8px"><h3>${esc(title)}</h3>${tag}</div>
      ${kv(rows)}${field}
      ${note ? `<div class="tiny muted" style="padding:8px 0 2px">${note}</div>` : ''}
      <div class="tiny" id="sheet-error" style="color:var(--bad);min-height:0"></div>
      <button class="btn block${danger ? ' danger' : ''}" type="button" id="sheet-ok" style="margin:12px 0 6px">${esc(okText)}</button>
      <button class="link" type="button" data-cancel style="display:block;margin:0 auto 6px">取消</button>`,
    (root, close) => {
      const ok = $('#sheet-ok', root);
      ok.onclick = async () => {
        const value = input ? $('#sheet-input', root).value.trim() : true;
        if (input && !value) { $('#sheet-error', root).textContent = input.required || '请先填写'; return; }
        ok.disabled = true;
        ok.textContent = '处理中…';
        try {
          const result = await onConfirm(value);
          haptic.ok();
          close(result || true);
        } catch (err) {
          if (fatal(err)) { close(false); return; }
          haptic.bad();
          const uncertain = !(err instanceof ApiError) || err.status === 0 || err.status >= 500;
          $('#sheet-error', root).textContent = uncertain ? `${err.message}。结果未确认，重试不会重复扣除。` : err.message;
          ok.disabled = false;
          ok.textContent = uncertain ? '重试（不会重复扣除）' : okText;
          if (!uncertain) { ok.disabled = true; }
        }
      };
      if (input) setTimeout(() => $('#sheet-input', root).focus(), 50);
    });
  }
  function rid() {
    const a = new Uint8Array(16);
    crypto.getRandomValues(a);
    return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
  }
  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const t = document.createElement('textarea');
      t.value = text;
      document.body.appendChild(t);
      t.select();
      try { document.execCommand('copy'); } catch (__) { /* best effort */ }
      t.remove();
    }
    haptic.tap();
    toast('已复制');
  }

  async function loadMe() {
    S.me = await api('/api/app/me');
    return S.me;
  }

  function showWelcome(ongoingItems = []) {
    const ongoingSection = ongoingItems.length ? `
      <div class="h" style="margin-top:24px"><span>进行中的抽奖</span><span>点击参与</span></div>
      <div id="welcome-ongoing">${ongoingItems.map(ongoingCardHtml).join('')}</div>` : '';
    setScreen(`
      ${stateView(icon('ticket'), '欢迎来到 Mini App', '从群抽奖消息按钮可直接参与活动，无需提前绑定。会员功能请先在 Bot 注册或绑定账号。')}
      ${ongoingSection}`);
    $$('#welcome-ongoing [data-lot-id]', screen).forEach(b => {
      b.onclick = () => {
        haptic.tap();
        push(a => lottery.detail(b.dataset.lotId, a));
      };
    });
  }

  function ongoingCardHtml(item) {
    const feeText = item.entry_fee > 0 ? `${item.entry_fee} 积分` : '免费';
    const joinedBadge = item.joined ? '<span class="pill green" style="font-size:10.5px;padding:2px 8px">已报名</span>' : '';
    return `<button class="card it" type="button" data-lot-id="${esc(item.id)}" style="display:flex;align-items:center;gap:12px;width:100%;text-align:left;margin-bottom:8px">
      <div class="ic sm" style="--c:#c07bff">${icon('ticket')}</div>
      <div class="grow">
        <div class="row between"><b class="small">${esc(item.title)}</b>${joinedBadge}</div>
        <div class="tiny muted" style="margin-top:2px">${esc(item.prize_summary || '丰厚奖品')}</div>
        <div class="tiny muted" style="margin-top:2px">${item.deadline ? '截止：' + stamp(item.deadline, true) : '开奖前均可报名'} · 费用：${feeText}</div>
      </div>
      <span class="go">›</span>
    </button>`;
  }

  // -- 我的 -------------------------------------------------------------------

  async function renderMe(alive) {
    if (!S.me) {
      loading();
      const [ctx, ongoingRes] = await Promise.all([
        api('/api/app/lottery/context'),
        api('/api/app/lottery/ongoing').catch(() => ({ items: [] }))
      ]);
      if (!alive()) return;
      if (!ctx.registered) {
        showWelcome(ongoingRes.items || []);
        return;
      }
    }
    const [me, ongoingRes] = await Promise.all([
      loadMe(),
      api('/api/app/lottery/ongoing').catch(() => ({ items: [] }))
    ]);
    if (!alive()) return;
    syncChrome();
    const lv = me.level;
    const tag = myTag();
    const cls = lvClass(tag);
    const next = lv.next_threshold;
    const nextLevel = me.levels.find((r) => r.level === lv.level + 1);
    const [statusCls, status] = STATUS[me.state] || ['', me.state];
    const today = me.today;
    const isToday = today.day === todayKey();
    const todayLabel = (today.points || today.growth)
      ? `${isToday ? '今日' : esc(today.day.slice(5))}积分`
      : '今日暂无奖励';
    const renew = me.renewal;
    let renewCard = '';
    if (!me.whitelist && renew.available) {
      const when = renew.window_open ? '现在可以续期' : (renew.opens_at ? `${stamp(renew.opens_at)} 起可续` : '');
      renewCard = `<div class="card">
        <div class="row"><div class="grow"><b>续期</b><div class="tiny muted"><div>到期 ${stamp(me.expires_at)}</div>${when ? `<div>${when}</div>` : ''}</div></div>
          <button class="btn${renew.blockers.length ? ' dis' : ''}" type="button" id="renew">续期 30 天</button></div>
        <div class="row" style="margin-top:12px;padding-top:12px;border-top:1px solid var(--line)">
          <div class="grow"><div>自动保号</div><div class="tiny muted"><div>到期前 24 小时内积分足够即自动续期</div><div>每次 ${num(renew.price)} 积分 / 30 天</div></div></div>
          <button class="switch${renew.auto_renew ? ' on' : ''}" type="button" id="auto" role="switch" aria-checked="${renew.auto_renew}" aria-label="自动保号"></button>
        </div></div>`;
    }

    const ongoingItems = ongoingRes.items || [];
    const ongoingSection = ongoingItems.length ? `
      <div class="h"><span>进行中的抽奖</span><span>点击参与</span></div>
      <div id="home-ongoing">${ongoingItems.map(ongoingCardHtml).join('')}</div>` : '';

    setScreen(`
      <div class="hero ${cls}"><div class="sheen"></div>
        <div class="row" style="justify-content:flex-start;gap:12px">
          ${face(me.name, me.avatar)}
          <div class="grow">
            <div style="font-weight:800;font-size:17px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(me.name)}</div>
            <div class="row" style="justify-content:flex-start;flex-wrap:wrap;gap:6px;margin-top:4px">
              ${lvTag(tag, true, true)}
              <span class="tiny nowrap" style="opacity:.85"><span class="status-dot ${statusCls}"></span>${esc(status)} · ${me.whitelist ? '永久有效' : esc(left(me.expires_at))}</span>
            </div>
          </div>
        </div>
        <div style="text-align:center;margin:12px 0 2px">${plaque(me.worn_title || lv.title, tag, false, lv)}</div>
        ${growthMeter(lv, nextLevel, today)}
      </div>
      <div class="stats">
        <div><b class="num">${num(me.balance)}</b><span>积分</span></div>
        <div><b class="num" style="color:${today.points ? 'var(--ok)' : 'var(--hint)'}">${today.points ? '+' + num(today.points) : '—'}</b><span>${todayLabel}</span></div>
        <div><b class="num">${Math.round((me.watch.seconds_30d || 0) / 3600)}<small style="font-size:11px"> 小时</small></b><span>近 30 天</span></div>
      </div>
      ${renewCard}
      ${ongoingSection}
      <div class="qgrid">
        <button type="button" data-go="lines"><div class="ic" style="--c:#6aa6ff">${icon('route')}</div>播放线路</button>
        <button type="button" data-go="level"><div class="ic" style="--c:#43d39e">${icon('shield')}</div>等级权益</button>
        <button type="button" data-go="titles"><div class="ic" style="--c:#ffb84d">${icon('award')}</div>我的称号</button>
        <button type="button" data-go="invites"><div class="ic" style="--c:#c07bff">${icon('user-plus')}</div>邀请</button>
      </div>
      <div class="list">
        ${me.is_admin ? '<button class="it" type="button" data-go="lottery"><div class="ic sm" style="--c:#c07bff">' + icon('trophy') + '</div><span class="t">抽奖管理</span><span class="go">›</span></button>' : ''}
        <button class="it" type="button" data-go="ledger"><div class="ic sm" style="--c:#ffcc5c">${icon('receipt')}</div><span class="t">积分流水</span><span class="go">›</span></button>
        <button class="it" type="button" data-go="devices"><div class="ic sm" style="--c:#6aa6ff">${icon('monitor-smartphone')}</div><span class="t">我的设备 <span class="tiny muted">${me.devices != null ? esc(me.devices) + (me.rights.max_devices ? ' / ' + esc(me.rights.max_devices) : '') + ' 台已登录' : ''}</span></span><span class="go">›</span></button>
        <button class="it" type="button" data-go="password"><div class="ic sm" style="--c:#43d39e">${icon('key-round')}</div><span class="t">修改密码</span><span class="go">›</span></button>
      </div>`);
    $$('[data-go]', screen).forEach((b) => {
      b.onclick = () => {
        haptic.tap();
        const go = b.dataset.go;
        if (go === 'invites') goTab('bag', { bag: 'invites' });
        else push({ lottery: lottery.list, lines: renderLines, level: renderLevel, titles: renderTitlesPage, ledger: renderLedger, devices: renderDevices, password: renderPassword }[go]);
      };
    });
    $$('#home-ongoing [data-lot-id]', screen).forEach(b => {
      b.onclick = () => {
        haptic.tap();
        push(a => lottery.detail(b.dataset.lotId, a));
      };
    });
    const renewBtn = $('#renew', screen);
    if (renewBtn) renewBtn.onclick = () => (renew.blockers.length ? toast(renew.blockers[0], true) : openRenew());
    const auto = $('#auto', screen);
    if (auto) auto.onclick = () => toggleAuto(!renew.auto_renew);
  }

  async function openRenew() {
    let preview;
    try {
      preview = await api('/api/app/renew/preview', { method: 'POST', body: {} });
    } catch (err) {
      if (!fatal(err)) toast(err.message, true);
      return;
    }
    if (preview.blockers && preview.blockers.length) { toast(preview.blockers[0], true); return; }
    const lv = preview.level;
    const done = await confirmSheet({
      title: '续期 30 天',
      tag: `<span class="lvtag ${S.me.whitelist ? 'wl' : 'lv' + lv.level}">${esc(lv.icon)} Lv${esc(lv.level)} 价格</span>`,
      rows: [['费用', `<b>${num(preview.price)} 积分</b>`],
        ['余额', `${num(preview.balance)} → <b>${num(preview.balance_after)}</b>`],
        ['当前到期', stamp(preview.expires_at, true)],
        ['续期后', `<b style="color:var(--ok)">${stamp(preview.new_expires_at, true)}</b>`]],
      note: '未到期从原到期日顺延；已到期从确认时起算。确认时会重新核对余额和价格，5 分钟内有效。',
      okText: `确认续期（扣 ${num(preview.price)} 积分）`,
      onConfirm: () => api('/api/app/renew/confirm', { method: 'POST', body: { nonce: preview.nonce } }),
    });
    if (done && done.ok) {
      toast(`续期成功，新到期 ${stamp(done.expires_at)}`);
      if (done.suspended) toast('续期成功。账号仍处于停用状态，续期不会解除停用，请联系管理员。');
      if (done.warning) toast(done.warning, true);
      render();
    }
  }

  async function toggleAuto(enabled) {
    const done = await confirmSheet({
      title: enabled ? '开启自动保号' : '关闭自动保号',
      note: enabled
        ? '到期前 24 小时内，如果积分足够，会按当时的等级价格自动扣积分续期 30 天。积分不足时不扣分，也不会续期。<br>账号到期后无法播放，超过 72 小时可能被删除。'
        : '关闭后不再自动扣积分续期；当前到期时间不变，仍可手动续期。',
      okText: enabled ? '确认开启' : '确认关闭',
      danger: !enabled,
      onConfirm: () => api('/api/app/auto-renew', { method: 'POST', body: { enabled, binding: S.me.binding } }),
    });
    if (done) {
      toast(enabled ? '自动保号已开启' : '自动保号已关闭');
      render();
    }
  }

  // -- 等级与权益 ---------------------------------------------------------------

  async function renderLevel(alive) {
    const me = S.me || await loadMe();
    if (!alive()) return;
    const lv = me.level;
    const tag = myTag();
    const next = lv.next_threshold;
    const r = me.rights;
    const rows = me.levels.filter((row) => row.level <= 7 || lv.level === 8).map((row) => {
      const reached = me.whitelist ? false : row.level < lv.level;
      const isCurrent = !me.whitelist && row.level === lv.level;
      const detail = isCurrent ? `续期 ${num(row.price)} 积分 / 30 天`
        : reached ? '已达成'
          : `${row.threshold != null ? num(row.threshold) + ' 成长 · ' : '专属授予 · '}${mbps(row.bandwidth_limit_kbps)} Mbps · 求片 ${row.request_quota}${row.max_devices ? ' · 设备 ' + row.max_devices : ''}`;
      return `<div class="it lv${row.level}${isCurrent ? ' cur' : (!reached ? ' dim-lv' : '')}">
        <span class="lvtag">${esc(emo(row.icon))} Lv${row.level}</span>
        <span class="t small">${isCurrent ? `<b>${esc(row.name)}</b>` : esc(row.name)} <span class="muted tiny">${esc(detail)}</span></span>
        ${isCurrent ? '<span class="chip cur">当前</span>' : reached ? '<span class="chip ok">✓</span>' : ''}</div>`;
    }).join('');
    setScreen(`
      <div class="hero ${lvClass(tag)}" style="text-align:center"><div class="sheen"></div>
        <div style="font-size:38px;filter:drop-shadow(0 4px 12px rgba(0,0,0,.4))">${me.whitelist ? '💠' : esc(emo(lv.icon))}</div>
        <div style="font-weight:800;font-size:19px;letter-spacing:2px">${me.whitelist ? '白名单' : `Lv${esc(lv.level)} · ${esc(lv.name)}`}</div>
        <div style="margin:6px 0 2px">${plaque(lv.title, tag, false, lv)}</div>
        ${growthMeter(lv, me.levels.find((row) => row.level === lv.level + 1), me.today)}
      </div>
      <div class="h"><span>当前权益</span></div>
      <div class="stats">
        <div><b class="num${r.bandwidth_kbps ? '' : ' txt-val'}">${r.bandwidth_kbps ? mbps(r.bandwidth_kbps) : '不限'}</b><span>带宽 Mbps</span></div>
        <div><b class="num${r.max_streams ? '' : ' txt-val'}">${r.max_streams || '不限'}</b><span>同时播放</span></div>
        <div><b class="num${r.request_quota ? '' : ' txt-val'}">${r.request_quota || '不限'}</b><span>每月求片</span></div>
      </div>
      <div class="card small row tri-row"><span>${r.max_devices ? `最多登录 ${esc(r.max_devices)} 台` : '设备不限'}</span><span class="${r.download ? '' : 'muted'}">${r.download ? '可下载' : '不可下载'}</span><span class="${r.transcode ? '' : 'muted'}">${r.transcode ? '可转码' : '不可转码'}</span></div>
      <div class="h"><span>观看时长</span></div>
      <div class="stats">
        ${[me.watch.seconds_24h, me.watch.seconds_30d, me.watch.total].map((s, i) => `<div><b class="num" style="font-size:16px">${dur(s).replace(/(\D+)/g, '<small>$1</small>')}</b><span>${['近 24 小时', '近 30 天', '累计'][i]}</span></div>`).join('')}
      </div>
      ${me.whitelist ? '' : `<div class="h"><span>升级之路</span></div><div class="list">${rows}</div>`}
      <div class="card small"><b>积分和成长怎么来？</b><div class="muted tiny" style="margin-top:4px">看片会自动获得积分和成长；签到等其他玩法见 Bot 的<span class="nowrap">「💡 帮助」</span>。</div></div>`);
  }

  // -- 称号 ---------------------------------------------------------------------

  async function titlesBody(target, alive) {
    loading(target);
    const [me, data] = await Promise.all([S.me ? Promise.resolve(S.me) : loadMe(), api('/api/app/titles')]);
    if (alive && !alive()) return;
    if (!data.available) { target.innerHTML = '<div class="empty">称号服务暂不可用</div>'; return; }
    const tag = myTag();
    const worn = data.rows.find((r) => r.worn);
    const levelTitle = me.level.title;
    const item = (label, sub, action, isWorn) => `<div class="it ${lvClass(tag)}">
        <span style="flex:1;min-width:0;overflow:hidden">${plaque(label, tag, false, me.level)}</span>
        <span class="tiny muted" style="text-align:right">${esc(sub)}</span>
        ${isWorn ? '<span class="chip ok">佩戴中</span>' : action}</div>`;
    target.innerHTML = `
      <div class="hero ${lvClass(tag)}" style="text-align:center;padding:22px 12px"><div class="sheen"></div>
        <div class="tiny" style="opacity:.75;letter-spacing:3px">佩 戴 中</div>
        <div style="margin:10px 0 8px">${plaque(worn ? worn.tag : levelTitle, tag, true, me.level)}</div>
        <div class="tiny" style="opacity:.8">${worn ? `自定义称号 · ${worn.expires_at ? stamp(worn.expires_at) + ' 到期' : '长期有效'}` : '等级称号 · 随等级变化'}</div>
      </div>
      <div class="h"><span>我的称号</span><span class="muted">佩戴、切换免费</span></div>
      <div class="list">
        ${item(levelTitle, '等级称号', '<button class="btn sm ghost" type="button" data-wear="">佩戴</button>', !worn)}
        ${data.rows.map((r) => item(r.tag, r.expires_at ? `${day(r.expires_at)} 到期` : '长期有效',
    `<button class="btn sm ghost" type="button" data-wear="${esc(r.id)}">佩戴</button>`, r.worn)).join('')}
      </div>
      <div class="card row"><div><b>创建新称号</b><div class="tiny muted">在商城兑换「自定义称号卡」后使用</div></div><button class="btn sm" type="button" data-shop>去兑换</button></div>
      <div class="card small"><b>群头衔</b><div class="tiny muted" style="margin-top:3px">佩戴后自动同步为群头衔；群头衔是纯文字，不带这里的样式。${data.sync.length ? '<br>当前：' + data.sync.map(esc).join('；') : ''}</div></div>
      <button class="link" type="button" data-gallery style="display:block;margin:0 auto">称号样式一览 ›</button>`;
    $$('[data-wear]', target).forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        try {
          await api('/api/app/titles/wear', { method: 'POST', body: { id: b.dataset.wear === '' ? null : Number(b.dataset.wear) } });
          haptic.ok();
          toast('已佩戴');
          titlesBody(target);
        } catch (err) {
          if (!fatal(err)) toast(err.message, true);
          b.disabled = false;
        }
      };
    });
    $('[data-shop]', target).onclick = () => goTab('bag', { bag: 'shop' });
    $('[data-gallery]', target).onclick = () => push(renderGallery);
  }
  async function renderTitlesPage(alive) {
    setScreen('<div id="titles-body"></div>');
    await titlesBody($('#titles-body'), alive);
  }
  async function renderGallery(alive) {
    const me = S.me || await loadMe();
    if (!alive()) return;
    const rows = me.levels.map((row) => `<div class="g1 lv${row.level}"><span class="lvtag">${esc(emo(row.icon))} Lv${row.level}</span>${plaque(row.title, { level: row.level }, false, row)}</div>`).join('');
    const sample = me.worn_title || me.level.title;
    setScreen(`<div class="tiny muted" style="margin:6px 4px 10px">各等级自带的称号和样式；自定义称号会按佩戴者的等级套用对应样式。</div>
      <div class="gallery">${rows}<div class="g1 wl"><span class="lvtag">💠 白名单</span>${plaque(sample, { whitelist: true }, false, me.level)}</div></div>
      <div class="card small" style="margin-top:12px"><div class="tiny muted">铭牌显示「等级名 + 标志 + 称号」，文字带本级配色的流光。Lv3 起铭牌外有光晕；Lv5 起光晕更强、边框发光；Lv7、Lv8 再加扫光和呼吸光晕，Lv8 最大最亮；白名单固定用水晶色。</div></div>`);
  }

  // -- 播放线路 / 积分流水 ---------------------------------------------------------

  async function renderLines(alive) {
    loading();
    const data = await api('/api/app/lines');
    if (!alive()) return;
    const lines = data.lines.map((l, i) => `<div class="card">
        <div class="row"><b>${esc(l.label || '线路')}</b><button class="btn sm ghost" type="button" data-copy="${i}">复制</button></div>
        <code class="u">${esc(l.url)}</code>${l.hint ? `<div class="tiny muted">${esc(l.hint)}</div>` : ''}</div>`).join('');
    setScreen(`
      <div class="card row"><span>当前在线</span><b class="num">${data.online == null ? '暂不可用' : esc(data.online) + ' 路播放'}</b></div>
      ${lines || '<div class="empty">暂无线路信息</div>'}
      ${data.note ? `<div class="card small" style="white-space:pre-wrap">${esc(data.note)}</div>` : ''}
      ${data.routing_rules ? '<div class="card small row"><span class="muted">分流规则配置文件在 Bot 的「播放线路」里获取，会以文件形式发到私聊。</span></div>' : ''}`);
    $$('[data-copy]', screen).forEach((b) => { b.onclick = () => copy(data.lines[Number(b.dataset.copy)].url); });
  }

  async function renderLedger(alive) {
    loading();
    const data = await api('/api/app/ledger');
    if (!alive()) return;
    const rows = data.rows.map((r) => `<div class="rrow"><span class="grow"><span class="small">${esc(r.reason || '变动')}</span>
        <div class="tiny muted">${stamp(r.at)} · 余额 ${num(r.balance)}</div></span>
        <b class="num" style="color:${r.delta >= 0 ? 'var(--ok)' : 'var(--bad)'}">${r.delta >= 0 ? '+' : ''}${num(r.delta)}</b></div>`).join('');
    setScreen(`<div class="card row" style="background:linear-gradient(135deg,color-mix(in srgb,#ffcc5c 22%,transparent),var(--glass))"><span>当前积分</span><b class="num" style="font-size:26px">${num(data.balance)}</b></div>
      <div class="h"><span>最近 30 条</span></div>
      <div class="list">${rows || '<div class="empty">还没有积分变动</div>'}</div>`);
  }

  // -- 设备 ---------------------------------------------------------------------

  const DEVICE_ICON = {
    phone: '<rect x="7" y="2.5" width="10" height="19" rx="2.2"/><path d="M11 18.5h2"/>',
    tablet: '<rect x="4.5" y="2.5" width="15" height="19" rx="2.2"/><path d="M11 18.5h2"/>',
    tv: '<rect x="2.5" y="4" width="19" height="12.5" rx="2"/><path d="M8 20h8M12 16.5V20"/>',
    desktop: '<rect x="3.5" y="4.5" width="17" height="11" rx="1.6"/><path d="M2 19h20"/>',
    web: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    other: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M9 12h6"/>',
  };
  function deviceIcon(kind) {
    return `<div class="dev-ic"><svg viewBox="0 0 24 24" aria-hidden="true">${DEVICE_ICON[kind] || DEVICE_ICON.other}</svg></div>`;
  }
  function guessPlatform(device) {
    if (!device) return '';
    const t = `${device.name} ${device.app}`.toLowerCase();
    if (/apple ?tv/.test(t)) return 'Apple TV';
    if (/iphone|ipad|ios/.test(t)) return 'iPhone/iPad';
    if (/\bmac/.test(t)) return 'Mac';
    if (/harmony|鸿蒙/.test(t)) return '鸿蒙';
    if (device.kind === 'tv') return '安卓电视';
    if (device.kind === 'desktop') return 'Windows';
    if (device.kind === 'phone' || device.kind === 'tablet') return '安卓手机';
    return '';
  }

  async function renderDevices(alive) {
    loading();
    const data = await api('/api/app/devices');
    if (!alive()) return;
    const devices = data.devices || [];
    const list = !data.available
      ? '<div class="empty">暂时读不到设备列表，请稍后再试</div>'
      : devices.length ? `<div class="list">${devices.map((d, i) => `<div class="it">${deviceIcon(d.kind)}
          <div class="t"><b class="small">${esc(d.name)}</b><div class="tiny muted">${esc([d.app, d.version].filter(Boolean).join(' '))}${d.app ? ' · ' : ''}${esc(ago(d.last_active))}</div></div>
          <button class="link danger-link" type="button" data-remove="${i}">移除</button></div>`).join('')}</div>`
        : '<div class="empty">还没有已登录的设备</div>';
    const platforms = data.players || [];
    const preferred = guessPlatform(devices[0]);
    let current = (platforms.find((p) => p.platform === preferred) || platforms[0] || {}).platform;
    const limit = data.limit;
    const used = data.available ? devices.length : null;
    const slots = limit ? `<div class="slots">${Array.from({ length: limit }, (_, i) => `<span class="${used != null && i < used ? 'on' : ''}"></span>`).join('')}</div>` : '';
    setScreen(`
      <div class="card">
        <div class="row" style="align-items:baseline;justify-content:flex-start;gap:6px">
          <b class="num" style="font-size:34px;line-height:1">${used ?? '—'}</b><span class="muted">${limit ? `/ ${esc(limit)} 台在用` : '台设备已登录'}</span></div>
        ${slots}
        <div class="tiny muted" style="margin-top:8px">${limit ? `最多同时登录 ${esc(limit)} 台，超出会自动登出最久没用的。` : ''}不认识或不再使用的设备可以移除，移除后它会立刻退出登录；之后用账号密码可以重新登录。</div>
      </div>
      <div class="h"><span>已登录</span><span>按最近使用排序</span></div>
      ${list}
      ${platforms.length ? `<div class="h"><span>推荐播放器</span></div>
        <div class="pills" id="platforms"></div><div id="players"></div>` : ''}`);
    $$('[data-remove]', screen).forEach((b) => {
      b.onclick = async () => {
        const d = devices[Number(b.dataset.remove)];
        const done = await confirmSheet({
          title: `移除 ${d.name}`,
          rows: [['设备', esc(d.name)], ['播放器', esc([d.app, d.version].filter(Boolean).join(' ') || '—')], ['最后使用', esc(ago(d.last_active))]],
          note: '移除后这台设备会立刻退出登录，正在播放的会中断；之后用账号密码可以重新登录。',
          okText: '移除并退出登录',
          danger: true,
          onConfirm: () => api('/api/app/devices/remove', { method: 'POST', body: { id: d.id } }),
        });
        if (!done) return;
        toast(`已移除 ${done.name || d.name}`);
        render();
      };
    });
    function drawPlayers() {
      const box = $('#platforms');
      if (!box) return;
      box.innerHTML = platforms.map((p) => `<button type="button" data-platform="${esc(p.platform)}" class="${p.platform === current ? 'on' : ''}">${esc(p.platform)}</button>`).join('');
      const group = platforms.find((p) => p.platform === current) || { players: [] };
      $('#players').innerHTML = `<div class="list">${group.players.map((p, i) => `<div class="it">
          <div class="t"><b class="small">${esc(p.name)}</b>${p.note ? `<div class="tiny muted">${esc(p.note)}</div>` : ''}</div>
          <button class="btn sm ghost" type="button" data-get="${i}">下载</button></div>`).join('')}</div>`;
      $$('[data-platform]', box).forEach((b) => { b.onclick = () => { haptic.tap(); current = b.dataset.platform; drawPlayers(); }; });
      $$('[data-get]', $('#players')).forEach((b) => {
        b.onclick = () => {
          const url = group.players[Number(b.dataset.get)].url;
          if (tg) tg.openLink(url); else window.open(url, '_blank', 'noopener');
        };
      });
    }
    drawPlayers();
  }

  // -- 修改密码 -------------------------------------------------------------------

  function randomPassword(length = 12) {
    const pool = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';
    const out = [];
    const buf = new Uint8Array(1);
    while (out.length < length) {
      crypto.getRandomValues(buf);
      if (buf[0] < 256 - (256 % pool.length)) out.push(pool[buf[0] % pool.length]);
    }
    return out.join('');
  }

  async function renderPassword(alive) {
    const me = S.me || await loadMe();
    if (!alive()) return;
    let mode = 'random';
    let generated = randomPassword();
    setScreen(`
      <div class="card"><div class="row"><span class="muted">用户名</span><span><b class="num">${esc(me.username)}</b>
        <button class="btn sm ghost" type="button" id="copy-user" style="margin-left:6px">复制</button></span></div></div>
      <div class="seg"><button type="button" data-mode="random" class="on">随机生成</button><button type="button" data-mode="custom">自己设置</button></div>
      <div id="pw-body"></div>
      <div class="tiny muted" style="margin:10px 4px">修改后旧密码立即失效，客户端里需要用新密码登录。</div>
      <div class="footer-action"><button class="btn block" type="button" id="pw-go">修改密码</button></div>`);
    $('#copy-user').onclick = () => copy(me.username);
    function draw() {
      $$('[data-mode]', screen).forEach((b) => b.classList.toggle('on', b.dataset.mode === mode));
      $('#pw-body').innerHTML = mode === 'random'
        ? `<div class="card"><div class="row"><span class="muted">新密码</span><button class="btn sm ghost" type="button" id="pw-new">换一个</button></div>
            <code class="u num" style="font-size:18px;letter-spacing:1px;text-align:center">${esc(generated)}</code>
            <div class="tiny muted">12 位字母和数字，修改成功后会再显示一次，记得保存。</div></div>`
        : `<div class="card">
            <input class="field" id="pw1" type="password" maxlength="128" placeholder="新密码，至少 6 位" autocomplete="new-password">
            <input class="field" id="pw2" type="password" maxlength="128" placeholder="再输入一次" autocomplete="new-password">
            <label class="tiny muted"><input type="checkbox" id="pw-show"> 显示密码</label>
            <div class="tiny muted" style="margin-top:4px">首尾空格也会原样保留。</div></div>`;
      if (mode === 'random') $('#pw-new').onclick = () => { generated = randomPassword(); haptic.tap(); draw(); };
      else {
        $('#pw-show').onchange = (e) => { $$('#pw1,#pw2', screen).forEach((f) => { f.type = e.target.checked ? 'text' : 'password'; }); };
      }
    }
    $$('[data-mode]', screen).forEach((b) => { b.onclick = () => { mode = b.dataset.mode; draw(); }; });
    draw();
    $('#pw-go').onclick = async () => {
      let password = generated;
      if (mode === 'custom') {
        password = $('#pw1').value;
        if (password.length < 6) { toast('密码至少 6 位', true); return; }
        if (password !== $('#pw2').value) { toast('两次输入的密码不一致', true); return; }
      }
      const done = await confirmSheet({
        title: '确认修改密码',
        rows: [['用户名', esc(me.username)], ['新密码', mode === 'random' ? `<b>${esc(password)}</b>` : '你刚才输入的密码']],
        note: '确认后旧密码立即失效。请先保存好新密码，不要转发给别人。',
        okText: '确认修改',
        onConfirm: async () => {
          try {
            return await api('/api/app/password', { method: 'POST', body: { password, mode } });
          } catch (err) {
            if (err instanceof ApiError && err.status > 0 && err.status < 500) throw err;
            // A lost response may still have changed it; keep the attempted value visible.
            throw new ApiError(err.status || 0, err.code, mode === 'random'
              ? `结果未确认，新密码可能已经生效：${password}` : '结果未确认，新密码可能已经生效');
          }
        },
      });
      if (!done) return;
      setScreen(`<div class="card" style="text-align:center;padding:22px 16px">
          <div style="font-size:32px;line-height:1;color:var(--ok);margin-bottom:8px">${icon('circle-check')}</div><b style="font-size:17px">密码已修改</b>
          <div class="kv" style="margin-top:12px"><span class="muted">用户名</span><b class="mono num">${esc(done.username)}</b></div>
          <div class="kv"><span class="muted">新密码</span><code class="mono num">${esc(password)}</code></div>
          <button class="btn block" type="button" id="copy-pw" style="margin-top:12px">复制新密码</button>
          ${(done.notices || []).map((n) => `<div class="banner" style="margin:12px 0 0;text-align:left">${icon('triangle-alert')}<span>${esc(n)}</span></div>`).join('')}
          <div class="tiny muted" style="margin-top:10px">请保存好新密码，不要转发。离开这个页面后不会再显示。</div></div>`);
      $('#copy-pw').onclick = () => copy(password);
    };
  }

  // -- 排行 ---------------------------------------------------------------------

  async function renderRank(alive) {
    const me = S.me || await loadMe();
    if (!alive()) return;
    const kinds = [['watch', '观影榜'], ...(me.features.points_ranking ? [['points', '积分榜']] : []), ['titles', '影片榜']];
    if (!kinds.some(([k]) => k === S.rank.kind)) S.rank.kind = 'watch';
    const periods = [['today', '今日'], ['yesterday', '昨日'], ['7d', '近 7 天']];
    setScreen(`
      <div class="seg">${kinds.map(([k, l]) => `<button type="button" data-kind="${k}" class="${S.rank.kind === k ? 'on' : ''}">${l}</button>`).join('')}</div>
      ${S.rank.kind === 'points' ? '' : `<div class="pills">${periods.map(([k, l]) => `<button type="button" data-period="${k}" class="${S.rank.period === k ? 'on' : ''}">${l}</button>`).join('')}</div>`}
      <div id="rank-body"></div>`);
    $$('[data-kind]', screen).forEach((b) => { b.onclick = () => { haptic.tap(); S.rank.kind = b.dataset.kind; render(); }; });
    $$('[data-period]', screen).forEach((b) => { b.onclick = () => { haptic.tap(); S.rank.period = b.dataset.period; render(); }; });
    const body = $('#rank-body');
    loading(body);
    const data = await api(`/api/app/rank?kind=${S.rank.kind}&period=${S.rank.period}`);
    if (!alive()) return;
    if (!data.available) { body.innerHTML = `<div class="empty">${esc(data.message || '排行统计暂时不可用，请稍后再试')}</div>`; return; }
    if (S.rank.kind === 'titles') { body.innerHTML = titleBoard(data); bindTitleBoard(body, data); return; }
    const value = (v) => (data.unit === 'points' ? num(v) : dur(v));
    const rows = data.rows;
    if (!rows.length) { body.innerHTML = '<div class="empty">暂无排行数据</div>'; return; }
    const pod = (r, cls) => {
      const rk = cls === 'g' ? 1 : cls === 's' ? 2 : 3;
      return r ? `<div class="p ${cls}">${cls === 'g' ? '<span class="crown">👑</span>' : ''}${face(r.name, r.avatar)}<span class="pod-rk num">${rk}</span>
        <div class="small"><b>${esc(r.name)}</b></div><div class="tiny num ${cls === 'g' ? 'gold' : 'muted'}">${value(r.value)}</div></div>` : '<div></div>';
    };
    const mine = data.mine;
    body.innerHTML = `
      <div class="podium">${pod(rows[1], 's')}${pod(rows[0], 'g')}${pod(rows[2], 'b')}</div>
      ${rows.length > 3 ? `<div class="list">${rows.slice(3).map((r) => `<div class="rrow"><span class="n">${r.rank}</span>${face(r.name, r.avatar, 'sm')}
        <span class="nm"><span class="nm-txt">${esc(r.name)}</span>${lvTag(r.tag, true)}</span><span class="tiny muted num">${value(r.value)}${data.unit === 'points' ? ' 积分' : ''}</span></div>`).join('')}</div>` : ''}
      ${data.unit === 'points' ? '<div class="tiny muted" style="margin:4px 4px 0">按当前可用积分排序。</div>' : data.stamp ? `<div class="tiny muted" style="margin:4px 4px 0">${esc(data.stamp)}</div>` : ''}
      <div class="mine">${face(me.name, me.avatar, 'sm')}<span class="grow">我的排名 <b>${mine ? '#' + mine.rank : '未上榜'}</b></span>
        <b class="num">${mine ? value(mine.value) + (data.unit === 'points' ? ' 积分' : '') : ''}</b></div>`;
  }
  function titleBoard(data) {
    const grid = (rows) => (rows.length ? `<div class="pgrid">${rows.map((r, i) => `<div>
        <button class="poster" type="button" style="--h:${hue(r.title)}" data-open="${i}">${posterHtml(r.poster, r.title, `<span class="rk">${r.rank}</span>`)}</button>
        <div class="pmeta num">${dur(r.seconds)} · ${r.plays} 次</div></div>`).join('')}</div>` : '<div class="empty">暂无数据</div>');
    return `<div class="h"><span>电影</span><span>${esc(data.stamp || '')}</span></div><div data-set="movies">${grid(data.movies)}</div>
      <div class="h"><span>剧集</span></div><div data-set="shows">${grid(data.shows)}</div>`;
  }
  function bindTitleBoard(body, data) {
    $$('[data-set]', body).forEach((set) => {
      $$('[data-open]', set).forEach((b) => {
        b.onclick = () => {
          const row = data[set.dataset.set][Number(b.dataset.open)];
          if (row.url) { if (tg) tg.openLink(row.url); else window.open(row.url, '_blank', 'noopener'); }
        };
      });
    });
  }

  // -- 求片 ---------------------------------------------------------------------

  function quotaCard(q) {
    return `<div class="card row" style="padding:10px 14px">
      <span class="small">${q.quota ? `本月剩余 <b class="num" style="font-size:18px">${q.remaining}</b> 次` : '本月求片<b> 不限次数</b>'}</span>
      <span class="tiny muted">提交扣 1 次 · 关注免费</span></div>`;
  }
  async function renderReq(alive) {
    const r = S.req;
    setScreen(`<div id="req-quota"></div>
      <form class="search" id="req-form">${icon('search')}<input id="req-q" type="search" enterkeyhint="search" placeholder="输入片名搜索" value="${esc(r.q)}" maxlength="100">
        <button class="btn sm" type="submit">搜索</button></form>
      <div class="pills">${[['', '全部'], ['movie', '电影'], ['tv', '剧集']].map(([k, l]) => `<button type="button" data-type="${k}" class="${r.type === k ? 'on' : ''}">${l}</button>`).join('')}</div>
      <div id="req-body"></div>`);
    $('#req-form').onsubmit = (e) => {
      e.preventDefault();
      const q = $('#req-q').value.trim();
      if (!q) { toast('请输入片名', true); return; }
      $('#req-q').blur();
      Object.assign(r, { q, results: null, page: 1 });
      render();
    };
    $$('[data-type]', screen).forEach((b) => {
      b.onclick = () => { r.type = b.dataset.type; r.results = null; r.page = 1; render(); };
    });
    const body = $('#req-body');
    if (!r.q) {
      loading(body);
      const mine = await api('/api/app/requests/mine');
      if (!alive()) return;
      $('#req-quota').innerHTML = quotaCard(mine);
      body.innerHTML = `<div class="h"><span>我的求片</span></div>${mine.rows.length ? `<div class="list">${mine.rows.map((row) => {
        const [cls, label] = REQ_STATUS[row.status] || ['', row.status_label || row.status];
        return `<div class="rrow"><span class="grow"><span class="small"><b>${esc(row.title || '未命名')}</b></span>
          <div class="tiny muted">${esc(row.media_label || '')}${row.demand_text ? ' · ' + esc(row.demand_text) : ''} · ${day(row.created_at)}${row.followed ? ' · 关注' : ''}</div></span>
          <span class="chip ${cls}">${esc(label)}</span></div>`;
      }).join('')}</div>` : '<div class="empty">还没有求过片。搜一下想看的电影或剧集吧。</div>'}`;
      return;
    }
    if (!r.results) {
      loading(body);
      const data = await api(`/api/app/requests/search?q=${encodeURIComponent(r.q)}&type=${r.type}&page=1`);
      if (!alive()) return;
      if (!data.available) { body.innerHTML = `<div class="empty">${esc(data.message)}</div>`; return; }
      Object.assign(r, { results: data.results, page: data.page, total: data.total_pages, quota: data });
    }
    $('#req-quota').innerHTML = quotaCard(r.quota);
    drawResults(body);
  }
  function drawResults(body) {
    const r = S.req;
    const badge = (x) => (x.in_library ? '<span class="bd in">已入库</span>'
      : x.requested === 'open' ? '<span class="bd follow">可关注</span>'
        : x.requested === 'accepted' ? '<span class="bd acc">已接受</span>' : '');
    body.innerHTML = r.results.length ? `<div class="pgrid">${r.results.map((x, i) => `<div>
        <button class="poster" type="button" style="--h:${hue(x.title)}" data-pick="${i}">${posterHtml(x.poster, x.title, badge(x))}</button>
        <div class="pmeta">${x.media_type === 'movie' ? '电影' : '剧集'}${x.year ? ' · ' + esc(x.year) : ''}</div></div>`).join('')}</div>
      ${r.page < r.total ? '<button class="btn ghost block" type="button" id="req-more" style="margin-top:12px">加载更多</button>' : ''}`
      : '<div class="empty">没有找到匹配的电影或剧集，换个关键词试试。</div>';
    $$('[data-pick]', body).forEach((b) => {
      b.onclick = () => { haptic.tap(); const x = r.results[Number(b.dataset.pick)]; push((alive) => renderTitle(alive, x)); };
    });
    const more = $('#req-more', body);
    if (more) {
      more.onclick = async () => {
        more.disabled = true;
        try {
          const data = await api(`/api/app/requests/search?q=${encodeURIComponent(r.q)}&type=${r.type}&page=${r.page + 1}`);
          r.results = r.results.concat(data.results || []);
          r.page = data.page;
          drawResults(body);
        } catch (err) {
          if (!fatal(err)) toast(err.message, true);
          more.disabled = false;
        }
      };
    }
  }

  async function renderTitle(alive, pick) {
    loading();
    const t = await api(`/api/app/requests/title?media_type=${pick.media_type}&tmdb_id=${pick.tmdb_id}`);
    if (!alive()) return;
    const title = t.title || pick.title;
    const tv = t.media_type === 'tv';
    const sel = new Set(t.seasons.filter((s) => s.state !== 'full').map((s) => s.season));
    let all = tv && t.seasons.length > 0 && sel.size === t.seasons.length && !t.seasons.some((s) => s.have);
    let key = rid();
    const lib = t.library;
    const libHtml = !lib.available ? '<div class="card small muted">媒体库暂时查不到，不确定是否已有；仍可提交。</div>'
      : lib.items.length ? `<div class="card small"><b>媒体库已有</b> ${lib.items.map((i) => `<span class="muted">${esc(i.name)}</span>${i.url ? ` <button class="link" type="button" data-url="${esc(i.url)}">去看 ↗</button>` : ''}`).join('；')}
          ${tv && t.seasons.some((s) => s.have) ? `<div class="tiny muted" style="margin-top:4px">${t.seasons.filter((s) => s.have).map((s) => `S${s.season} ${s.state === 'full' ? '全' : s.have + '/' + s.episodes}`).join(' · ')}</div>` : ''}</div>`
        : '<div class="card small muted">媒体库里还没有这部。</div>';
    const poster = t.poster || pick.poster || '';
    setScreen(`
      ${poster ? `<div class="backdrop" style="background-image:url('${esc(poster)}')"></div>` : ''}
      <div class="row" style="align-items:flex-end;gap:12px;${poster ? 'margin-top:-92px;' : ''}margin-bottom:12px;position:relative;justify-content:flex-start">
        <div class="poster${poster ? '' : ' no-img'}" style="--h:${hue(title)};width:100px;flex:none">${posterHtml(poster, '')}</div>
        <div class="grow"><div style="font-weight:600;font-size:17px;line-height:1.25;margin-bottom:4px">${esc(title)}</div>
          <div class="tiny muted" style="line-height:1.35">${t.original_title && t.original_title !== title ? esc(t.original_title) + ' · ' : ''}${t.year ? esc(t.year) + ' · ' : ''}${tv ? `剧集${t.seasons.length ? ' · 共 ' + t.seasons.length + ' 季' : ''}` : '电影'} · <button class="link tiny nowrap" type="button" id="tmdb" style="padding:0">TMDB ↗</button></div></div>
      </div>
      ${t.overview ? `<div class="tiny muted clamp" id="overview" style="margin-bottom:10px">${esc(t.overview)}</div>` : ''}
      ${libHtml}
      ${tv && t.seasons.length ? '<div class="h"><span>要求哪些季</span><span>已入库的季默认不选</span></div><div class="pills" id="seasons"></div>' : ''}
      <div class="card small" id="summary"></div>
      ${t.history.length ? `<div class="h"><span>之前的求片</span></div><div class="list">${t.history.map((h) => {
    const [cls, label] = REQ_STATUS[h.status] || ['', h.status_label];
    return `<div class="rrow"><span class="grow small">#${esc(h.id)} ${esc(h.demand_text || '')}</span><span class="chip ${cls}">${esc(label)}</span></div>`;
  }).join('')}</div>` : ''}
      <div class="footer-action"><button class="btn block" type="button" id="submit" disabled>检查中…</button></div>`);
    $('#tmdb').onclick = () => {
      const url = `https://www.themoviedb.org/${tv ? 'tv' : 'movie'}/${t.tmdb_id}`;
      if (tg) tg.openLink(url); else window.open(url, '_blank', 'noopener');
    };
    const ov = $('#overview');
    if (ov) ov.onclick = () => ov.classList.toggle('clamp');
    $$('[data-url]', screen).forEach((b) => { b.onclick = () => (tg ? tg.openLink(b.dataset.url) : window.open(b.dataset.url, '_blank', 'noopener')); });

    let state = null;
    let checkSeq = 0;
    function seasonsLabel() {
      if (!tv) return '电影';
      if (all || !t.seasons.length) return '全部季';
      const list = [...sel].sort((a, b) => a - b);
      if (!list.length) return '未选择';
      const ranges = [];
      for (const n of list) {
        const last = ranges[ranges.length - 1];
        if (last && n === last[1] + 1) last[1] = n; else ranges.push([n, n]);
      }
      return '第 ' + ranges.map(([a, b]) => (a === b ? a : `${a}–${b}`)).join('、') + ' 季';
    }
    function drawSeasons() {
      const box = $('#seasons');
      if (!box) return;
      box.innerHTML = `<button type="button" data-all class="${all ? 'on' : ''}">全部季</button>` + t.seasons.map((s) => `<button type="button" data-s="${s.season}"
        class="${!all && sel.has(s.season) ? 'on' : ''}${s.state === 'full' ? ' have' : ''}">S${s.season}${s.state === 'full' ? ' 已有' : s.state === 'partial' ? ' 部分' : ''}</button>`).join('');
      $('[data-all]', box).onclick = () => { all = !all; changed(); };
      $$('[data-s]', box).forEach((b) => {
        b.onclick = () => {
          const n = Number(b.dataset.s);
          all = false;
          if (sel.has(n)) sel.delete(n); else sel.add(n);
          changed();
        };
      });
    }
    function payload() {
      return { media_type: t.media_type, tmdb_id: t.tmdb_id, seasons: tv && !all ? [...sel].sort((a, b) => a - b) : null };
    }
    function drawSummary() {
      const s = state;
      const btn = $('#submit');
      let cost = '—';
      let label = '提交求片';
      let enabled = false;
      let action = 'create';
      if (tv && !all && !sel.size) {
        label = t.seasons.length && t.seasons.every((x) => x.state === 'full') ? '已全部入库' : '请先选择季';
      } else if (s && s.same && s.same.status === 'open') {
        action = s.same.mine ? 'mine' : 'create';
        cost = s.same.mine ? '你已提交过' : '免费关注（已有人求过）';
        label = s.same.mine ? '你已提交过，查看进度' : '免费关注';
        enabled = true;
      } else if (s && s.same) {
        cost = '—';
        label = '同样的需求已接受，等待入库';
      } else if (s && s.quota && s.remaining <= 0) {
        cost = '本月次数已用完';
        label = '本月次数已用完';
      } else if (s) {
        cost = s.quota ? `1 次（${s.remaining} → ${s.remaining - 1}）` : '不限次数';
        label = '提交求片（扣 1 次）';
        enabled = true;
      }
      $('#summary').innerHTML = `<div class="row"><span class="muted">本次</span><b>${esc(seasonsLabel())}</b></div>
        <div class="row" style="margin-top:4px"><span class="muted">扣次数</span><b class="num">${esc(cost)}</b></div>`;
      btn.textContent = label;
      btn.disabled = !enabled;
      btn.dataset.action = action;
    }
    async function changed() {
      drawSeasons();
      state = null;
      drawSummary();
      if (tv && !all && !sel.size) return;
      const seq = ++checkSeq;
      try {
        const res = await api('/api/app/requests/check', { method: 'POST', body: payload() });
        if (seq !== checkSeq || !alive()) return;
        state = res;
      } catch (err) {
        if (fatal(err)) return;
        state = null;
        toast(err.message, true);
      }
      drawSummary();
    }
    $('#submit').onclick = async () => {
      if ($('#submit').dataset.action === 'mine') { S.req.q = ''; S.req.results = null; goTab('req'); return; }
      const follow = state && state.same && state.same.status === 'open';
      const submit = () => api('/api/app/requests', { method: 'POST', body: { ...payload(), create_key: key } });
      let done;
      if (follow) {
        $('#submit').disabled = true;
        try { done = await submit(); } catch (err) {
          if (!fatal(err)) toast(err.message, true);
          $('#submit').disabled = false;
          return;
        }
      } else {
        done = await confirmSheet({
          title: '确认求片',
          rows: [['影片', esc(title)], ['范围', esc(seasonsLabel())],
            ['扣次数', state.quota ? `1 次（剩 ${state.remaining} → ${state.remaining - 1}）` : '不限次数']],
          note: '提交后管理员会处理；入库后 Bot 会私聊通知你。',
          okText: '提交求片（扣 1 次）',
          onConfirm: submit,
        });
      }
      if (!done) return;
      haptic.ok();
      key = rid();
      toast(done.created ? '已提交，扣 1 次' : done.followed ? '已关注原单，没有扣次数' : '已有同样的需求，没有重复扣次数');
      S.req.q = '';
      S.req.results = null;
      goTab('req');
    };
    if (tv && t.seasons.length && t.seasons.every((s) => s.state === 'full')) sel.clear();
    changed();
  }

  // -- 背包 ---------------------------------------------------------------------

  async function renderBag(alive) {
    const me = S.me || await loadMe();
    if (!alive()) return;
    const segs = [['shop', '商城'], ['items', '道具'], ['titles', '称号'], ['invites', '邀请']];
    setScreen(`
      <div class="card row" style="background:linear-gradient(135deg,color-mix(in srgb,#ffcc5c 22%,transparent),var(--glass))">
        <span>积分 <button class="link tiny" type="button" id="to-ledger">流水 ›</button></span><b class="num" style="font-size:26px" id="bag-balance">${num(me.balance)}</b></div>
      <div class="seg">${segs.map(([k, l]) => `<button type="button" data-seg="${k}" class="${S.bag === k ? 'on' : ''}">${l}</button>`).join('')}</div>
      <div id="bag-body"></div>`);
    $('#to-ledger').onclick = () => push(renderLedger);
    $$('[data-seg]', screen).forEach((b) => { b.onclick = () => { haptic.tap(); S.bag = b.dataset.seg; render(); }; });
    const body = $('#bag-body');
    if (S.bag === 'titles') { await titlesBody(body, alive); return; }
    loading(body);
    if (S.bag === 'shop') await shopBody(body, alive);
    else if (S.bag === 'items') await itemsBody(body, alive);
    else await invitesBody(body, alive);
  }
  async function refreshBalance() {
    try { await loadMe(); const el = $('#bag-balance'); if (el) el.textContent = num(S.me.balance); } catch (_) { /* shown on next render */ }
  }

  async function shopBody(body, alive) {
    const data = await api('/api/app/shop');
    if (!alive()) return;
    if (!data.available) { body.innerHTML = '<div class="empty">商城暂未开放</div>'; return; }
    const term = (it) => it.description || (it.duration_days ? `使用后 ${it.duration_days} 天有效` : '长期有效');
    body.innerHTML = data.items.length ? `<div class="list">${data.items.map((it, i) => {
      const [glyph, color] = KIND_ICON[it.kind] || KIND_ICON.custom;
      const soldOut = it.limit && it.redeemed >= it.limit;
      const short = it.price - data.balance;
      const btn = soldOut ? '<span class="btn sm dis">已兑完</span>'
        : short > 0 ? `<span class="btn sm dis">还差 ${num(short)}</span>`
          : `<button class="btn sm" type="button" data-buy="${i}">兑换</button>`;
      return `<div class="it"><div class="ic" style="--c:${color}">${icon(glyph)}</div>
        <div class="t"><b class="small">${esc(it.name)}</b><div class="tiny muted">${esc(term(it))}${it.limit ? ` · 每人限 ${it.limit} 次` : ''}</div></div>
        <div style="text-align:right"><div class="tiny num"><b>${num(it.price)}</b> 积分</div><div style="margin-top:4px">${btn}</div></div></div>`;
    }).join('')}</div>` : '<div class="empty">商城里暂时没有商品</div>';
    $$('[data-buy]', body).forEach((b) => {
      b.onclick = async () => {
        const it = data.items[Number(b.dataset.buy)];
        const key = rid();
        const done = await confirmSheet({
          title: '确认兑换',
          rows: [['商品', esc(it.name)], ['费用', `<b>${num(it.price)} 积分</b>`],
            ['余额', `${num(data.balance)} → <b>${num(data.balance - it.price)}</b>`]],
          note: `放入背包，使用时才生效。${it.duration_days ? `生效后持续 ${it.duration_days} 天。` : '长期有效。'}`,
          okText: `确认兑换（扣 ${num(it.price)} 积分）`,
          onConfirm: () => api(`/api/app/shop/${it.id}/redeem`, { method: 'POST', body: { request_id: key, expected_spec: it.spec } }),
        });
        if (!done) return;
        toast(`兑换成功：${done.name || it.name}，余额 ${num(done.balance)}`);
        if (done.notice) toast(done.notice, true);
        await refreshBalance();
        shopBody(body, alive);
      };
    });
  }

  async function itemsBody(body, alive) {
    const data = await api('/api/app/items');
    if (!alive()) return;
    if (!data.available) { body.innerHTML = '<div class="empty">背包暂不可用</div>'; return; }
    const now = Date.now() / 1000;
    const rows = data.rows.slice().sort((a, b) => ((a.used_at == null ? 0 : 1) - (b.used_at == null ? 0 : 1)) || b.id - a.id);
    body.innerHTML = rows.length ? `<div class="list">${rows.map((r, i) => {
      const [glyph, color] = KIND_ICON[r.kind] || KIND_ICON.custom;
      let side;
      let sub;
      if (r.kind === 'custom') {
        sub = r.expires_at ? `保留到 ${stamp(r.expires_at)}` : '';
        side = '<span class="chip">说明已发到 Bot</span>';
      } else if (r.used_at == null) {
        sub = r.duration_days ? `生效后 ${r.duration_days} 天` : '长期有效';
        side = `<button class="btn sm" type="button" data-use="${i}">${r.kind === 'title_card' ? '创建称号' : '使用'}</button>`;
      } else if (r.active) {
        sub = r.expires_at ? `${stamp(r.expires_at)} 到期` : '长期有效';
        side = '<span class="chip ok">生效中</span>';
      } else {
        sub = r.expires_at && r.expires_at <= now ? `${day(r.expires_at)} 已到期` : '已使用';
        side = '<span class="chip">已失效</span>';
      }
      return `<div class="it"><div class="ic" style="--c:${color}">${icon(glyph)}</div>
        <div class="t"><b class="small">${esc(r.name)}</b> <span class="tiny muted">#${esc(r.id)}</span><div class="tiny muted">${esc(sub)}</div>
          ${r.error ? `<div class="tiny" style="color:var(--warn)">${esc(r.error)}</div>` : ''}</div>${side}</div>`;
    }).join('')}</div>` : '<div class="empty">背包是空的。在商城兑换的道具会放在这里。</div>';
    $$('[data-use]', body).forEach((b) => {
      b.onclick = async () => {
        const r = rows[Number(b.dataset.use)];
        const titleCard = r.kind === 'title_card';
        const done = await confirmSheet({
          title: titleCard ? '创建称号' : `使用 ${r.name}`,
          rows: titleCard ? [] : [['道具', esc(r.name)], ['生效', r.duration_days ? `立即生效，持续 ${r.duration_days} 天` : '立即生效，长期有效']],
          input: titleCard ? { placeholder: '称号文字', max: 16, hint: '1–16 个字，不含表情和链接，不得冒充管理。创建后即消耗这张卡，之后佩戴、切换都免费。', required: '请填写称号文字' } : null,
          okText: titleCard ? '创建称号' : '确认使用',
          onConfirm: (value) => api(`/api/app/items/${r.id}/use`, { method: 'POST', body: titleCard ? { title: value } : {} }),
        });
        if (!done) return;
        toast(titleCard ? '称号已创建，可以去佩戴了' : (done.granted || '已生效'));
        if (done.warning) toast(done.warning, true);
        await refreshBalance();
        itemsBody(body, alive);
      };
    });
  }

  async function invitesBody(body, alive) {
    const [data, me] = await Promise.all([api('/api/app/invites'), S.me ? Promise.resolve(S.me) : loadMe()]);
    if (!alive()) return;
    if (!data.available) { body.innerHTML = '<div class="empty">邀请功能暂未开放</div>'; return; }
    const paused = data.suspended_until;
    const usable = data.codes.filter((c) => c.usable);
    const statusLabel = { active: '正常', suspended: '已停用', expired: '已到期', pending: '待开通', exhausted: '已超额' };
    const nodeLine = (n) => (n.status === 'deleted'
      ? `<span class="chip bad" style="font-size:10px">已删除</span><div class="tiny muted">${day(n.deleted_at)} 被删除</div>`
      : `${lvTag(n.tag, true)}<div class="tiny muted">${esc(statusLabel[n.status] || n.status)}${n.active_7d === true ? ' · 近 7 天活跃' : n.active_7d === false ? ' · 近 7 天未观看' : ''} · 注册 ${day(n.registered_at)} · 到期 ${n.expires_at ? day(n.expires_at) : '永久'}</div>`);
    const dot = (n) => (n.status === 'deleted' ? 'bad' : n.status === 'active' && n.active_7d !== false ? '' : 'off');
    const tree = data.nodes.map((n, i) => `<div class="node${i ? ' top' : ''}" style="${i ? 'border-top:1px solid var(--line)' : ''}"><span class="dot ${dot(n)}"></span>
        ${face(n.label, n.avatar, n.status === 'deleted' ? 'sm del' : 'sm')}
        <div class="t"><b class="small">${esc(n.label)}</b> ${nodeLine(n)}</div></div>
      ${n.children.length ? `<div class="tree">${n.children.map((c) => `<div class="node"><span class="dot ${dot(c)}"></span>
        <div class="t small">${esc(c.label)} <span class="tiny muted">${c.status === 'deleted' ? '已删除' : esc(statusLabel[c.status] || c.status)}${c.status !== 'deleted' && c.active_7d === true ? ' · 活跃' : ''}</span></div></div>`).join('')}
        ${n.children.length < n.descendants ? `<div class="tiny muted" style="padding:2px 0 8px">下面还有 ${n.descendants - n.children.length} 人</div>` : ''}</div>` : ''}`).join('');
    const codes = usable.length ? usable.map((c, i) => `
        <code class="u num">${esc(c.code)}</code>
        <div class="row"><span class="tiny muted">${paused ? '暂时不能使用' : `剩 ${c.uses_left} 次 · ${c.expires_at ? stamp(c.expires_at) + ' 到期' : '长期有效'}`}</span>
          <span><button class="btn sm ghost" type="button" data-copycode="${i}"${paused ? ' disabled' : ''}>复制</button>
          <button class="btn sm" type="button" data-share="${i}"${paused ? ' disabled' : ''}>分享给好友</button></span></div>`).join('')
      : '<div class="tiny muted" style="margin-top:6px">还没有可用的邀请码。</div>';
    body.innerHTML = `
      ${paused ? `<div class="banner bad">${icon('circle-pause')}<span><b>邀请资格暂停至 ${stamp(paused)}</b><br><span class="muted">期间不能生成新码，已发出的码也暂时不能用。</span></span></div>` : ''}
      <div class="card"${paused ? ' style="opacity:.6"' : ''}>
        <div class="row"><b>我的邀请码</b><span class="chip ${!paused && data.quota > 0 ? 'acc' : ''}">${paused ? '暂停中' : `剩余名额 ${data.quota ?? '—'}`}</span></div>
        ${codes}
        ${!paused && data.quota > 0 ? '<button class="btn ghost block" type="button" id="mint" style="margin-top:10px">生成邀请码（用 1 个名额）</button>' : ''}
        ${!paused && !data.quota && !usable.length ? '<div class="tiny muted" style="margin-top:6px">没有邀请名额。可以在商城兑换「邀请码卡」。</div>' : ''}
      </div>
      <div class="h"><span>我邀请的人 · 名下共 ${data.total} 人</span></div>
      <div class="list" style="padding:4px 14px">${tree || '<div class="empty">你还没有邀请过别人</div>'}</div>
      <div class="banner">${icon('triangle-alert')}<span>你邀请的人如果被管理员删号或因违规被删号，管理员可能连带处理你的账号，或暂停你的邀请资格。请只邀请信得过的人。</span></div>`;
    $$('[data-copycode]', body).forEach((b) => { b.onclick = () => copy(usable[Number(b.dataset.copycode)].code); });
    $$('[data-share]', body).forEach((b) => {
      b.onclick = () => {
        const code = usable[Number(b.dataset.share)].code;
        const link = me.bot_username ? `https://t.me/${me.bot_username}` : '';
        const text = `邀请码：${code}\n打开 Bot 发送 /start，按提示用邀请码注册。`;
        const url = `https://t.me/share/url?url=${encodeURIComponent(link || code)}&text=${encodeURIComponent(text)}`;
        if (tg) tg.openTelegramLink(url); else copy(text);
      };
    });
    const mint = $('#mint', body);
    if (mint) {
      mint.onclick = async () => {
        const done = await confirmSheet({
          title: '生成邀请码',
          rows: [['消耗', '1 个邀请名额'], ['剩余名额', `${data.quota} → ${data.quota - 1}`]],
          note: '每个邀请码只能用一次。你邀请的人违规，你可能要承担连带责任。',
          okText: '确认生成',
          onConfirm: () => api('/api/app/invites', { method: 'POST' }),
        });
        if (!done) return;
        toast(`新邀请码：${done.code}`);
        invitesBody(body, alive);
      };
    }
  }

  const lottery = window.createLotteryUI({ api, esc, setScreen, push, back, toast, stamp, tg, icon });
  const games = window.createGamesUI({ api, esc, setScreen, push, back, toast, sheet, closeLayer, icon, lvTag, lvClass, plaque, myTag, getProfile: () => S.me, tg });
  const TABS = { me: renderMe, rank: renderRank, req: renderReq, games: games.lobby, bag: renderBag };

  if (!tg || !tg.initData) {
    setScreen(stateView(icon('smartphone'), '请在 Telegram 里打开', '会员面板需要从 Bot 首页的「📱 会员面板」按钮进入。'));
    return;
  }
  const params = new URLSearchParams(location.search);
  const start = tg.initDataUnsafe?.start_param || params.get('tgWebAppStartParam') || '';
  const lotteryLink = /^lottery_([a-f0-9]{24})$/.exec(start);
  if (lotteryLink) {
    // Routing is not authority. Each lottery API independently verifies initData.
    S.stack.push({ render: alive => lottery.detail(lotteryLink[1], alive) });
    render();
  } else {
    Promise.all([
      api('/api/app/lottery/context'),
      api('/api/app/lottery/ongoing').catch(() => ({ items: [] }))
    ]).then(async ([ctx, ongoingRes]) => {
      if (!ctx.registered) {
        showWelcome(ongoingRes.items || []);
        return;
      }
      const deepLink = { devices: renderDevices, password: renderPassword }[params.get('page')];
      const gameLink = /^gc_([A-Za-z0-9_]+)$/.exec(start);
      if (gameLink || start === 'games' || !deepLink) {
        // Explicit links only preview a table. Reopening restores an already seated member.
        const tableId = gameLink ? gameLink[1] : await games.activeTable();
        if (tableId || start === 'games') {
          await loadMe();
          S.tab = 'games';
          if (tableId) S.stack.push({ render: alive => games.table(tableId, alive) });
        }
      }
      if (deepLink && !gameLink && start !== 'games') S.stack.push({ render: deepLink });
      render();
    }).catch(err => { if (!fatal(err)) setScreen(failView(err)); });
  }
})();
