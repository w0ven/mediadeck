/* Game Center: server-authoritative views. No dealing, timers or money are simulated here. */
'use strict';
window.createGamesUI = ({
  api, esc, setScreen, push, back, toast, sheet, closeLayer,
  icon, lvTag, lvClass, plaque, myTag, getProfile, tg,
}) => {
  const ROOT = '/api/app/games';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const num = n => Number(n || 0).toLocaleString('zh-CN');
  const signed = n => (n > 0 ? '+' : n < 0 ? '−' : '') + num(Math.abs(n || 0));
  const names = { zjh: '炸金花', holdem: '德州扑克', niuniu: '牛牛', doudizhu: '斗地主', gomoku: '五子棋', xiangqi: '象棋' };
  const tierClasses = { practice: 'tp', novice: 't1', normal: 't2', pro: 't3', master: 't4', supreme: 't5' };
  const isBoard = game => game === 'xiangqi' || game === 'gomoku';
  const levels = [['easy', '入门'], ['normal', '普通'], ['hard', '困难']];
  const img = (key, cls = '', alt = '') => `<img class="gc-art ${cls}" src="/app/assets/game-assets/${key}.webp" alt="${esc(alt)}">`;
  const unit = t => t.currency === 'beans' ? '欢乐豆' : '积分';
  const funds = (w, t) => Number(w?.[t.currency === 'beans' ? 'beans' : 'points'] || 0);
  const reqId = () => crypto.randomUUID ? crypto.randomUUID()
    : Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
  const get = path => api(ROOT + path);
  let catalog = null, wallet = null, current = null, busy = false, pending = null;
  let sheetGeneration = 0;
  const tierCache = new Map();
  const PROPS = [
    ['flower', '🌹', '送鲜花', 2, 20, '花瓣漫天'],
    ['poop', '💩', '丢便便', 3, 30, '糊脸暴击'],
    ['champagne', '🥂', '敬香槟', 5, 50, '举杯致敬'],
    ['bomb', '💣', '丢炸弹', 5, 50, '轰炸震屏'],
  ];
  const PHRASES = [
    ['hurry', '快点吧，我等到花儿都谢了！'],
    ['mercy', '底裤都输光啦，大佬手下留情！'],
    ['allin', '搏一搏，单车变摩托！'],
    ['strong', '这把牌稳了，跟不跟？'],
    ['lucky', '承让承让，运气好而已～'],
    ['bluff', '别装了，我知道你在偷鸡！'],
    ['battle', '不要走，决战到天亮！'],
    ['tatian', '顺为凡，逆则仙，梭哈就在一念间！'],
  ];
  // Sound is decoration: browsers allow it only after a tap, and a missing file never blocks play.
  const sound = (() => {
    const base = '/app/assets/game-assets/audio/', buffers = new Map(), rawBytes = new Map();
    const ALL_SFX = ['deal', 'flip', 'chip', 'raise', 'fold', 'check', 'allin', 'turn', 'win', 'lose', 'tap', 'tick', 'flower', 'poop', 'champagne', 'bomb', 'chat'];
    let ctx = null, track = null, wanted = false, unlocked = false;
    const pref = kind => { try { return localStorage.getItem('gc-sound-' + kind) !== 'off'; } catch { return true; } };
    function sync() {
      const on = wanted && pref('music') && document.visibilityState === 'visible';
      if (on && !track) { track = new Audio(base + 'music.mp3'); track.loop = true; track.volume = .45; }
      if (on) track.play().catch(() => {});
      else track?.pause();
      if (document.visibilityState === 'visible' && ctx && ctx.state !== 'running') {
        ctx.resume().catch(() => {});
      }
    }
    function unlock(fromGesture = false) {
      const Context = window.AudioContext || window.webkitAudioContext;
      if (!ctx && Context) ctx = new Context();
      if (ctx) {
        if (ctx.state !== 'running') ctx.resume().catch(() => {});
        // iOS WKWebView (Telegram iOS) needs a 1-sample silent buffer started inside a user gesture
        // so later async SSE events (like "your turn") can play through Web Audio without being muted.
        if (fromGesture && !unlocked) {
          try {
            const silent = ctx.createBuffer(1, 1, 22050);
            const src = ctx.createBufferSource();
            src.buffer = silent;
            src.connect(ctx.destination);
            src.start(0);
            unlocked = true;
          } catch { /* ignore */ }
          // Eagerly decode critical turn/countdown cues as soon as AudioContext unlocks.
          for (const key of ['turn', 'tick', 'deal', 'chip', 'raise']) load(key);
        }
      }
      sync();
    }
    function fetchBytes(name) {
      if (!rawBytes.has(name)) {
        rawBytes.set(name, fetch(base + name + '.mp3').then(r => r.ok ? r.arrayBuffer() : null).catch(() => null));
      }
      return rawBytes.get(name);
    }
    function load(name) {
      if (!ctx) return Promise.resolve(null);
      if (!buffers.has(name)) {
        buffers.set(name, fetchBytes(name).then(data => data
          ? new Promise(ok => ctx.decodeAudioData(data.slice(0), ok, () => ok(null)))
          : null).catch(() => null));
      }
      return buffers.get(name);
    }
    function preload() {
      for (const name of ALL_SFX) fetchBytes(name);
      if (ctx) for (const name of ALL_SFX) load(name);
    }
    function fallbackPlay(name, volume = 1) {
      try {
        const a = new Audio(base + name + '.mp3');
        a.volume = Math.max(0, Math.min(1, volume));
        a.play().catch(() => {});
      } catch { /* ignore */ }
    }
    async function play(name, volume = 1) {
      if (!pref('sfx') || document.visibilityState !== 'visible') return;
      unlock(false);
      if (ctx) {
        if (ctx.state !== 'running') {
          try { await ctx.resume(); } catch { /* ignore */ }
        }
        if (ctx.state === 'running') {
          const buffer = await load(name);
          if (buffer && ctx.state === 'running') {
            const source = ctx.createBufferSource(), gain = ctx.createGain();
            source.buffer = buffer; gain.gain.value = volume;
            source.connect(gain).connect(ctx.destination);
            source.start();
            return;
          }
        }
      }
      fallbackPlay(name, volume);
    }
    function burst(name, count = 1, gap = 105, volume = 1) {
      const n = Math.max(1, Math.min(5, count));
      for (let i = 0; i < n; i++) {
        if (i === 0) play(name, volume);
        else setTimeout(() => play(name, volume * .92), i * gap);
      }
    }
    function set(kind, on) {
      try { localStorage.setItem('gc-sound-' + kind, on ? 'on' : 'off'); } catch { /* private mode keeps defaults */ }
      sync();
    }
    const gestureUnlock = () => unlock(true);
    document.addEventListener('pointerdown', gestureUnlock, true);
    document.addEventListener('touchstart', gestureUnlock, { capture: true, passive: true });
    document.addEventListener('click', gestureUnlock, true);
    document.addEventListener('visibilitychange', sync);
    return { play, burst, preload, pref, set, music(on) { wanted = on; sync(); } };
  })();
  // Telegram WebApp HapticFeedback + Web Vibration API fallback; safely no-ops when user turns haptics off.
  const haptic = (() => {
    const enabled = () => sound.pref('haptic');
    const apiHaptic = () => tg?.HapticFeedback || window.Telegram?.WebApp?.HapticFeedback || null;
    const vib = pattern => { try { navigator.vibrate?.(pattern); } catch { /* ignore */ } };
    let lastSelectAt = 0;
    function impact(style = 'light') {
      if (!enabled()) return;
      const h = apiHaptic();
      if (h?.impactOccurred) { try { h.impactOccurred(style); return; } catch { /* ignore */ } }
      vib(style === 'heavy' ? 40 : style === 'medium' ? 24 : 12);
    }
    function notify(type = 'success') {
      if (!enabled()) return;
      const h = apiHaptic();
      if (h?.notificationOccurred) { try { h.notificationOccurred(type); return; } catch { /* ignore */ } }
      vib(type === 'error' ? [30, 45, 30] : type === 'warning' ? [25, 40, 25] : [18, 30, 24]);
    }
    function select(throttleMs = 0) {
      if (!enabled()) return;
      if (throttleMs > 0) {
        const nowTs = performance.now();
        if (nowTs - lastSelectAt < throttleMs) return;
        lastSelectAt = nowTs;
      }
      const h = apiHaptic();
      if (h?.selectionChanged) { try { h.selectionChanged(); return; } catch { /* ignore */ } }
      vib(8);
    }
    function turnAlert() {
      if (!enabled()) return;
      const h = apiHaptic();
      if (h?.impactOccurred) {
        try {
          h.impactOccurred('heavy');
          setTimeout(() => { try { h.impactOccurred('medium'); } catch { /* ignore */ } }, 140);
          return;
        } catch { /* ignore */ }
      }
      vib([35, 65, 55]);
    }
    return { impact, notify, select, turnAlert };
  })();
  // Hold'em chip display mode: chips vs big blinds (persisted per browser).
  let bbMode = (() => { try { return localStorage.getItem('gc-holdem-bb') === '1'; } catch { return false; } })();
  function setBbMode(on) {
    bbMode = !!on;
    try { localStorage.setItem('gc-holdem-bb', bbMode ? '1' : '0'); } catch { /* ignore */ }
  }
  function fmtChips(amount, t, forceChips = false) {
    const n = Number(amount || 0);
    if (!forceChips && bbMode && t?.game === 'holdem' && t.rules?.big_blind > 0) {
      const bb = n / t.rules.big_blind;
      return `${Number.isInteger(bb) ? bb : bb.toFixed(1).replace(/\.0$/, '')} BB`;
    }
    return num(n);
  }
  const HOLDEM_ACTION_ZH = { fold: '弃牌', check: '过牌', call: '跟注', bet: '下注', raise: '加注', all_in: '全下' };
  const STREET_ZH = { preflop: '翻牌前', flop: '翻牌', turn: '转牌', river: '河牌', showdown: '摊牌' };
  function holdemPositionName(seatNum, t, hand = t.hand) {
    const e = hand?.engine;
    if (!e || !e.seats?.length) return '';
    const n = e.seats.length;
    if (e.button === seatNum) return n === 2 ? 'BTN/SB' : 'BTN';
    if (e.small_blind_seat === seatNum) return 'SB';
    if (e.big_blind_seat === seatNum) return 'BB';
    const ordered = [...e.seats].sort((a, b) => ((a.seat - e.button - 1 + 6) % 6) - ((b.seat - e.button - 1 + 6) % 6));
    const idx = ordered.findIndex(s => s.seat === seatNum);
    if (idx < 0) return '';
    const fromEnd = n - 1 - idx;
    if (fromEnd === 1) return 'CO';
    if (fromEnd === 2) return 'HJ';
    return 'UTG';
  }
  function clearRunoutTimers(c) {
    if (!c?.runoutTimers) return;
    for (const id of c.runoutTimers) clearTimeout(id);
    c.runoutTimers = [];
  }

  function dispose() {
    if (current) {
      current.disposed = true;
      stopStream(current);
      clearInterval(current.clock);
      clearTimeout(current.celebrateTimer);
      clearTimeout(current.autoSheetTimer);
      clearTimeout(current.turnAlertTimer);
      clearInterval(current.sheetTicker);
      clearRunoutTimers(current);
      current = null;
    }
    sound.music(false);
    $('#screen').classList.remove('gc-scene', 'gc-table-screen');
    if ($('.gc-sheet')) closeLayer();
    sheetGeneration++;
  }
  function showSheet(html, bind) {
    const generation = ++sheetGeneration;
    return sheet(`
      <div class="gc gc-sheet">${html}</div>`, (root, close) => {
      const own = () => generation === sheetGeneration && root.isConnected && !$('#layer').hidden;
      bind?.(root, value => { if (own()) close(value); }, own);
    });
  }
  function pageTop(title) {
    return `
      <header class="gc-page-top">
        <button class="gc-back" type="button" data-gc-back aria-label="返回">‹</button>
        <h2>${esc(title)}</h2>
        <button class="link" type="button" data-gc-rules>玩法</button>
      </header>`;
  }
  function walletHtml(w) {
    return `
      <div class="gc-wallet">
        <div class="tiny muted">可用积分</div>
        <b class="num">${num(w?.points)}</b>
        <div class="tiny">已冻结 ${num(w?.frozen)}</div>
      </div>`;
  }
  function pendingHtml() {
    return pending && !busy ? '<div class="gc-notice" role="status">上次操作结果未确认。重试使用原凭证，不会重复扣分。<button class="btn sm" data-gc-pending>重试原操作</button></div>' : '';
  }
  function bindPending(root) {
    const b = $('[data-gc-pending]', root);
    if (b) b.onclick = () => pending?.retry();
  }
  function buttonsBusy() {
    $$('[data-gc-write]').forEach(b => { if (busy) b.disabled = true; });
  }
  // Keep the same intent on an uncertain transport/server failure. Never guess success with a new key.
  async function write(path, body, success, alive = () => true) {
    const signature = JSON.stringify([path, body]);
    if (busy) return false;
    if (pending && pending.signature !== signature) { toast('请先重试上次未确认的操作', true); return false; }
    if (!pending) pending = { signature, path, body: { ...body, request_id: reqId() } };
    const intent = pending;
    intent.retry = () => write(path, body, success, alive);
    busy = true;
    buttonsBusy();
    try {
      const response = await api(ROOT + path, { method: 'POST', body: intent.body });
      pending = null;
      if (alive()) await success(response);
      else toast('操作已处理，回到游戏页可查看最新状态');
      return true;
    } catch (err) {
      const uncertain = !err.status || err.status >= 500 && err.status !== 501 && err.status !== 503;
      if (!uncertain) pending = null;
      if (alive()) {
        toast(uncertain ? `${err.message}。结果未确认，请重试原操作。` : err.message, true);
        if (current && (err.status === 409 || err.code === 'insufficient_funds')) await refresh(current).catch(() => {});
      }
      return false;
    } finally {
      busy = false;
      if (current && live(current)) paintTable(current);
      $$('[data-gc-retry-slot]').forEach(el => { el.innerHTML = pendingHtml(); bindPending(el); });
      $$('[data-gc-write]').forEach(b => { b.disabled = b.dataset.gcDisabled === 'true'; });
    }
  }
  const disabled = yes => `data-gc-write data-gc-disabled="${!!yes}"${yes || busy ? ' disabled' : ''}`;

  async function activeTable() {
    try {
      const data = await get('/me');
      wallet = data.wallet;
      return data.active_tables?.[0]?.table_id || null;
    } catch (_) {
      return null; // Games being disabled must not disable the rest of the member panel.
    }
  }
  async function lobby(alive) {
    const data = await get('/lobby');
    if (!alive()) return;
    catalog = data; wallet = data.wallet;
    $('#screen').classList.add('gc-scene');
    const featured = ['zjh', 'holdem'].map(id => {
      const g = data.games.find(x => x.id === id);
      if (!g) return '';
      const available = data.enabled && g.enabled && g.available;
      return `
        <article class="gc-feature ${id === 'holdem' ? 'gc-holdem-feature' : ''}" data-gc-game="${id}">
        ${img('emblem-' + id, 'gc-feature-art')}
          <div class="grow">
            <h2 class="gc-feature-name">${names[id]} <span class="gc-play-tag">${available ? '可玩' : g.available ? '未开放' : '待开放'}</span></h2>
            <div class="gc-feature-desc">${id === 'zjh' ? '2–5 人一桌 · 闷牌 看牌 跟注 比牌' : '2–6 人一桌 · 无限注 · 主池与边池'}</div>
            <div class="gc-feature-meta">${available ? '<i class="gc-dot"></i>' : ''}${id === 'zjh' ? `在桌 ${num(g.seated)} 人 · 不抽水` : '连续发牌 · 自选带入筹码'}</div>
            <div class="gc-feature-buttons">
              <button class="btn sm" data-gc-quick="${id}" ${disabled(!available)}>快速开始</button>
              <button class="btn sm ghost" data-gc-tiers="${id}">选场次</button>
            </div>
          </div>
        </article>`;
    }).join('');
    const boards = ['xiangqi', 'gomoku'].map(id => {
      const g = data.games.find(x => x.id === id);
      const available = !!(g && data.enabled && g.enabled && g.available);
      return `
        <article class="gc-board-card" data-gc-game="${id}">${img('emblem-' + id, 'gc-board-art')}
          <div class="grow">
            <b>${names[id]}</b> <span class="gc-play-tag">${available ? '可玩' : g?.available ? '未开放' : '待开放'}</span>
            <div class="tiny muted">${id === 'xiangqi' ? '1v1 · 红先黑后' : '1v1 · 15 路自由五子'} · 可陪机器人练习</div>
            <div class="gc-feature-buttons">
              <button class="btn sm" data-gc-quick="${id}" ${disabled(!available)}>快速开始</button>
              <button class="btn sm ghost" data-gc-tiers="${id}">选场次</button>
            </div>
          </div>
        </article>`;
    }).join('');
    const soon = ['niuniu', 'doudizhu'].map((id, i) => `
      <button class="gc-game-card" data-gc-soon>${img('emblem-' + id)}<span><b>${names[id]}</b><span class="tiny muted">${['坐庄比牌', '三人对战'][i]}</span><em>即将上线</em></span></button>`).join('');
    setScreen(`
      <section class="gc gc-lobby">
        <header class="gc-head">
          <div>
            <h1>游戏中心</h1>
            <div class="small muted">和会员同桌对打，用 Bot 积分</div>
          </div>
          ${walletHtml(wallet)}
        </header>
        ${!data.enabled ? '<div class="gc-notice">游戏中心暂未开放；已在桌的会员仍可回桌完成对局、离桌结算。</div>' : ''}
        <div data-gc-retry-slot>${pendingHtml()}</div>
        ${(data.active_tables || []).map(t => `
          <button class="gc-resume" data-gc-table="${esc(t.table_id)}">${icon('rotate-cw')} 回到原桌 · ${esc(names[t.game] || t.game)} <span>›</span></button>`).join('')}
      ${featured}
        <div class="h">棋类对弈</div>
        <div class="gc-boards">${boards}</div>
        <div class="h"><span>正在等人</span>
          <button class="link" data-gc-broadcast>群播报说明</button>
        </div>
        <div class="list" id="gc-waiting">
          <div class="empty">正在加载牌桌…</div>
        </div>
        <div class="h">更多游戏</div>
        <div class="gc-grid">${soon}</div>
        <div class="gc-beans">${img('bean')}<span>欢乐豆 <b class="num">${num(wallet.beans)}</b><small>每天补足到 ${num(wallet.daily_beans)} · 只用于练习，不能换积分</small></span></div>
        <div class="gc-rule-links">
          <button class="link" data-gc-rules="zjh">炸金花玩法</button>
          <button class="link" data-gc-rules="holdem">德州扑克玩法</button>
          <button class="link" data-gc-rules="xiangqi">象棋玩法</button>
          <button class="link" data-gc-rules="gomoku">五子棋玩法</button>
        </div>
      </section>`);
    const root = $('.gc-lobby');
    $$('[data-gc-tiers]', root).forEach(b => b.onclick = () => push(a => tiers(b.dataset.gcTiers, a)));
    $$('[data-gc-quick]', root).forEach(b => b.onclick = async () => {
      b.disabled = true;
      try {
        const ts = await loadTiers(b.dataset.gcQuick);
        if (alive()) enter(b.dataset.gcQuick, ts.find(t => t.id === 'normal') || ts[0], 'quick', alive);
      } catch (e) { toast(e.message, true); }
      finally { if (b.isConnected) b.disabled = false; }
    });
    $$('[data-gc-soon]', root).forEach(b => b.onclick = () => toast('即将上线，敬请期待'));
    $$('[data-gc-rules]', root).forEach(b => b.onclick = () => rules(b.dataset.gcRules));
    $('[data-gc-broadcast]', root).onclick = broadcast;
    bindTables(root); bindPending(root);
    const lists = await Promise.all(['zjh', 'holdem', 'xiangqi', 'gomoku'].map(g => loadTiers(g).catch(() => [])));
    if (!alive()) return;
    $('#gc-waiting').innerHTML = waitingHtml(lists.flat());
    bindTables($('#gc-waiting'));
  }
  async function loadTiers(game) {
    const data = await get(`/${game}/tiers`);
    tierCache.set(game, data.tiers);
    return data.tiers;
  }
  function waitingHtml(ts) {
    const tables = ts.flatMap(t => (t.waiting_tables || []).map(w => ({ ...w, tier_info: t })));
    return tables.length ? tables.map(t => `
      <div class="it gc-waiting-row"><span class="gc-tier-chip ${tierClasses[t.tier] || ''}">${esc(t.tier_info.name)}</span>
        <div class="grow"><b>${esc(t.host_name)} 开的桌</b>
          <div class="tiny muted">${esc(names[t.game])} · ${num(t.players)}/${num(t.max_players)} 人</div>
        </div>
        <button class="btn sm" data-gc-table="${esc(t.table_id)}">看桌</button>
      </div>`).join('') : '<div class="empty">还没有等人的桌，开一桌邀请朋友吧</div>';
  }
  function bindTables(root) { $$('[data-gc-table]', root).forEach(b => b.onclick = () => openTable(b.dataset.gcTable)); }
  function tierDetails(game, t) {
    if (isBoard(game)) return `每局押 ${num(t.stake)} ${unit(t)} · 赢家全拿 · 和棋退回`;
    if (game === 'holdem') return `盲注 ${num(t.small_blind)}/${num(t.big_blind)} · 带入 ${num(t.buy_min)}${t.buy_max == null ? ' 起，不设上限' : '–' + num(t.buy_max)}`;
    return `底注 ${num(t.ante)} · ${t.cap == null ? '单局不封顶' : '封顶 ' + num(t.cap)}`;
  }
  async function tiers(game, alive) {
    const [ts, w, c] = await Promise.all([loadTiers(game), get('/wallet'), get('/lobby')]);
    if (!alive()) return;
    wallet = w; catalog = c;
    $('#screen').classList.add('gc-scene');
    const g = c.games.find(x => x.id === game);
    const available = c.enabled && g?.enabled && g?.available;
    const rows = ts.map(t => {
      const short = funds(w, t) < t.min_balance;
      return `
        <article class="gc-tier ${tierClasses[t.id] || ''}" data-gc-tier="${esc(t.id)}">
          <div class="gc-tier-badge">${img('tier-' + t.id)}<b>${esc(t.name.replace('场', ''))}<small>${t.id === 'supreme' && !isBoard(game) ? '无上限' : t.currency === 'beans' ? '欢乐豆' : ''}</small></b></div>
          <div class="grow"><b class="gc-tier-title">${t.currency === 'beans' ? '机器人陪玩' : esc(t.name)}</b>
            <div class="small">${esc(tierDetails(game, t))}</div>
            <div class="tiny muted">${short ? `还差 ${num(t.min_balance - funds(w, t))} ${unit(t)}` : t.currency === 'beans' ? '只花欢乐豆，不花积分' : `在桌 ${num(t.seated)} 人 · 需 ${num(t.min_balance)} 可用积分`}</div>
          </div>
          <div class="gc-tier-actions">
            <button class="btn sm" data-gc-enter="quick" ${disabled(!available || short)}>${!available ? '暂未开放' : short ? '余额不足' : t.id === 'practice' ? '开始练习' : '快速开始'}</button>
            ${t.id !== 'practice' ? `
              <button class="btn sm ghost" data-gc-enter="create" ${disabled(!available || short)}>开桌</button>` : ''}
          </div>
        </article>`;
    }).join('');
    setScreen(`
      <section class="gc gc-tiers">${pageTop(names[game] + ' · 选场次')}
        <div class="gc-balance-line">可用 ${num(w.points)} 积分 · 已冻结 ${num(w.frozen)}<span>${img('bean')} ${num(w.beans)}</span></div>
        ${!available ? '<div class="gc-notice">当前场次暂未开放。</div>' : ''}
        <div data-gc-retry-slot>${pendingHtml()}</div>
        ${rows}
        <div class="gc-note">${game === 'zjh'
          ? '入座不扣分；开局冻结单局封顶，结算立即退回没用完的部分。'
            + '<br>至尊场需 2,500 可用积分（以场次参数为准），每手冻结全部可用积分，输赢没有固定封顶。'
          : isBoard(game) ? '入座不扣分；开局时双方各冻结一份押注，赢家拿走两份，和棋或开局作废原额退回。超时判负，掉线时本步放宽到 2 分钟。'
          : '确认带入后冻结为桌上筹码；两手之间可补码，离桌退回剩余。至尊场带入不设上限，但不能超过可用余额。'}
          <br>积分场只有真人；练习场机器人陪玩。全场不抽水。
        </div>
        <div class="h"><span>等人中的桌</span>
          <button class="link" data-gc-broadcast>群播报说明</button>
        </div>
        <div class="list">${waitingHtml(ts)}</div>
      </section>`);
    const root = $('.gc-tiers');
    $('[data-gc-back]', root).onclick = back;
    $('[data-gc-rules]', root).onclick = () => rules(game);
    $('[data-gc-broadcast]', root).onclick = broadcast;
    $$('[data-gc-enter]', root).forEach(b => b.onclick = () => enter(game, ts.find(t => t.id === b.closest('[data-gc-tier]').dataset.gcTier), b.dataset.gcEnter, alive));
    bindTables(root); bindPending(root);
  }
  function enter(game, tier, mode, alive) {
    if (!tier) return;
    const path = mode === 'create' ? '/tables' : '/quick-start';
    const body = { game, tier: tier.id };
    const success = data => {
      closeLayer();
      openTable(data.table.id, data.table);
      toast(data.created ? '已开桌，你是房主' : '已回到牌桌');
    };
    if (game === 'holdem') return buySheet(tier, wallet, false, 0, amount => write(path, { ...body, buy_in: amount }, success, alive));
    if (isBoard(game)) return boardEnter(game, tier, mode, path, body, success, alive);
    showSheet(`
      <h3>${mode === 'create' ? '开桌' : '快速开始'} · ${esc(tier.name)}</h3>
      <p>${esc(tierDetails(game, tier))}</p>
      <div class="gc-note">入座不扣分。${tier.cap == null ? '至尊场每手冻结全部可用积分，没有固定封顶。'
        : `开局时冻结 ${num(tier.cap)} ${unit(tier)}，结算退回未使用部分。`}
        <br>${tier.currency === 'beans' ? '机器人陪玩，只使用欢乐豆。'
          : '找不到空桌时会新开一桌，你成为房主。群播报由管理员配置，未开启时不会发卡。'}
      </div>
      <div data-gc-retry-slot>${pendingHtml()}</div>
      <button class="btn block" data-gc-confirm ${disabled(false)}>确认${mode === 'create' ? '开桌' : '开始'}</button>
      <button class="link gc-close" data-cancel>取消</button>`, root => {
      $('[data-gc-confirm]', root).onclick = () => write(path, body, success, alive);
      bindPending(root);
    });
  }
  function boardEnter(game, tier, mode, path, body, success, alive) {
    const practice = tier.currency === 'beans';
    let level = 'normal';
    const clock = tier.total_seconds ? `每方 ${Math.round(tier.total_seconds / 60)} 分钟、每步 ${num(tier.step_seconds)} 秒，超时判负。` : '超时判负。';
    showSheet(`
      <h3>${practice ? '陪机器人练习' : mode === 'create' ? '开桌' : '快速开始'} · ${names[game]}</h3>
      <p>${esc(tierDetails(game, tier))}</p>${practice ? `
      <div class="tiny muted">机器人难度</div>
      <div class="seg gc-levels">${levels.map(([v, l]) => `<button data-gc-level="${v}" class="${v === level ? 'on' : ''}">${l}</button>`).join('')}</div>` : ''}
      <div class="gc-note">${practice ? '只用欢乐豆，入门难度适合刚学的朋友；练习场可以悔棋。'
        : '开局时冻结押注，赢家拿走双方押注，和棋退回，不抽水。找不到空桌时会新开一桌，你成为房主。'}<br>${clock}</div>
      <div data-gc-retry-slot>${pendingHtml()}</div>
      <button class="btn block" data-gc-confirm ${disabled(false)}>${practice ? '开始练习' : mode === 'create' ? '确认开桌' : '确认开始'}</button>
      <button class="link gc-close" data-cancel>取消</button>`, root => {
      $$('[data-gc-level]', root).forEach(b => b.onclick = () => {
        level = b.dataset.gcLevel;
        $$('[data-gc-level]', root).forEach(x => x.classList.toggle('on', x === b));
      });
      $('[data-gc-confirm]', root).onclick = () => write(path, practice ? { ...body, robot_level: level } : body, success, alive);
      bindPending(root);
    });
  }
  function openTable(id, snapshot = null) { push(alive => table(id, alive, snapshot)); }
  const live = c => current === c && !c.disposed && c.alive();
  async function table(id, alive, initial = null) {
    const data = initial ? { table: initial } : await get('/tables/' + encodeURIComponent(id));
    if (!alive()) return;
    const c = current = {
      id, alive, table: null, disposed: false, stream: null,
      generation: 0, attempt: 0, status: 'connecting', offset: 0, notice: '', history: [],
      preAction: null, raiseBar: null, runout: null, runoutTimers: [], boardAnimFrom: 0,
      draggingRaise: false, deferredPaint: false, turnAlertTimer: null,
    };
    $('#screen').classList.add('gc-scene', 'gc-table-screen');
    sound.preload();
    sound.music(true);
    accept(c, data.table, true);
    c.clock = setInterval(() => { if (!live(c)) return; tick(c); }, 250);
    connect(c, !!initial);
  }
  async function refresh(c) {
    const data = await get('/tables/' + encodeURIComponent(c.id));
    if (live(c)) accept(c, data.table, true);
  }
  function stopStream(c) {
    c.generation++;
    if (c.stream) c.stream.close();
    c.stream = null;
    clearTimeout(c.retry); clearTimeout(c.renew);
  }
  function streamStatus(c, value) {
    c.status = value;
    const el = $('[data-gc-connection]');
    if (el && live(c)) {
      el.textContent = {
        live: '实时连接', connecting: '连接中…', retry: '连接中断 · 自动重连',
        paused: '已暂停连接', error: '连接不可用 · 点此重试',
      }[value];
      el.dataset.state = value;
    }
  }
  async function connect(c, pull = true) {
    stopStream(c);
    if (!live(c) || document.hidden || c.table?.phase === 'closed') return;
    const generation = c.generation;
    // A scheduled token renewal keeps the healthy label until the new stream arrives.
    if (c.attempt >= 3) streamStatus(c, 'retry');
    else if (c.status !== 'live') streamStatus(c, 'connecting');
    try {
      if (pull) await refresh(c);
      if (!live(c) || c.generation !== generation || document.hidden) return;
      const token = await api(`${ROOT}/tables/${encodeURIComponent(c.id)}/stream-token`, { method: 'POST', body: {} });
      if (!live(c) || c.generation !== generation || document.hidden) return;
      // Only the same-origin stream endpoint may receive this short-lived credential.
      const url = new URL(token.url, location.origin);
      if (url.origin !== location.origin || url.pathname !== `${ROOT}/tables/${c.id}/events`) throw new Error('实时连接地址无效');
      const es = c.stream = new EventSource(url.href);
      es.addEventListener('snapshot', event => {
        if (!live(c) || c.generation !== generation) return;
        try {
          const data = JSON.parse(event.data);
          accept(c, data.table);
          c.attempt = 0;
          streamStatus(c, 'live');
        } catch (_) {
          reconnect(c);
        }
      });
      es.onerror = () => { if (live(c) && c.generation === generation) reconnect(c); };
      c.renew = setTimeout(() => connect(c), Math.max(1000, (token.expires_at - now(c) - 2) * 1000));
    } catch (err) {
      if (!live(c) || c.generation !== generation) return;
      if ([401, 403, 404].includes(err.status)) {
        stopStream(c);
        streamStatus(c, 'error');
        toast(err.message, true);
      } else reconnect(c);
    }
  }
  function reconnect(c) {
    stopStream(c);
    if (!live(c) || document.hidden) return;
    const delay = Math.min(15000, 800 * 2 ** Math.min(c.attempt++, 5));
    // Tolerate a dropped stream and two reconnect attempts before showing a warning.
    // A received snapshot resets the streak, including a stream that ended at token expiry.
    streamStatus(c, c.attempt >= 3 ? 'retry' : 'connecting');
    c.retry = setTimeout(() => connect(c), delay);
  }
  document.addEventListener('visibilitychange', () => {
    if (!current) return;
    if (document.hidden) { stopStream(current); streamStatus(current, 'paused'); }
    else connect(current);
  });
  window.addEventListener('pagehide', () => { if (current) stopStream(current); });
  window.addEventListener('pageshow', e => { if (e.persisted && current) connect(current); });
  window.addEventListener('online', () => { if (current) connect(current); });
  const now = c => Date.now() / 1000 + c.offset;
  // Adapt the server's Hold'em view for display only. Never infer missing legal actions,
  // hidden hole cards, payouts or best-five evaluations from the public table state.
  function holdemHand(h) {
    if (!h?.engine) return h;
    const e = h.engine, result = h.result || e.results;
    const legal = h.legal_actions ?? e.legal_actions ?? {}, options = {};
    if (legal.fold) options.fold = {};
    if (legal.check) options.check = {};
    if (legal.call > 0) options.call = { amount: legal.call };
    if (legal.min_raise_to != null) {
      options[e.current_bet ? 'raise' : 'bet'] = { min: legal.min_raise_to, max: legal.max_raise_to };
    }
    if (legal.all_in > 0) options.all_in = { amount: legal.all_in };
    const players = (e.seats || []).map(p => ({
      ...p, cards: p.hole, shown: !!p.shown, folded: p.status === 'folded', all_in: p.status === 'all_in',
      invested: p.committed, hand_name: p.hand_name || result?.players?.[p.player]?.hand || null,
      best5: p.best5 || (result?.players?.[p.player]?.shown ? result.players[p.player].best5 : null),
    }));
    return {
      ...h, current_player: h.current_player ?? e.current_player,
      engine: { ...e, players }, legal_actions: options,
      result: result ? {
        ...result, net: Object.fromEntries(Object.entries(result.players).map(([id, p]) => [id, p.net])),
        winners: [...new Set(result.pots.flatMap(p => p.winners))],
      } : null,
    };
  }
  function accept(c, t, force = false) {
    if (!live(c) || !t || t.id !== c.id) return;
    if (c.table && (t.revision < c.table.revision || !force && t.revision === c.table.revision)) return;
    const previous = c.table;
    if (t.game === 'holdem') t = { ...t, hand: holdemHand(t.hand), last_hand: holdemHand(t.last_hand) };
    c.offset = Number(t.server_time) - Date.now() / 1000;
    c.table = t; wallet = t.me.wallet;
    if (t.last_hand) {
      const idx = c.history.findIndex(h => h.number === t.last_hand.number);
      if (idx >= 0) c.history[idx] = t.last_hand;
      else c.history.unshift(t.last_hand);
    }
    c.history = c.history.slice(0, 10);
    // Keep inline raise panel in sync with turn; clear pre-action on hand/street change or unexpected bet increase.
    if (t.game === 'holdem') {
      if (!t.hand || c.raiseBar && (c.raiseBar.handNo !== t.hand.number || c.raiseBar.turn !== t.hand.turn || t.hand.current_player !== t.me.player)) {
        c.raiseBar = null;
      }
      if (!t.hand || c.preAction && (c.preAction.handNo !== t.hand.number || c.preAction.street !== t.hand.engine?.street)) {
        c.preAction = null;
      } else if (c.preAction?.kind === 'call_exact' && t.hand) {
        const meSeat = handPlayers(t, t.hand).find(x => x.player === t.me.player);
        const curNeed = Math.max(0, (t.hand.engine?.current_bet || 0) - (meSeat?.bet || 0));
        if (curNeed > c.preAction.amount) c.preAction = null;
      }
    }
    const preOp = resolvePreAction(c, t);
    react(c, previous, t, !!preOp);
    if (c.draggingRaise && t.game === 'holdem' && t.hand && c.raiseBar
      && t.hand.number === c.raiseBar.handNo && t.hand.turn === c.raiseBar.turn
      && t.hand.current_player === t.me.player) {
      // Avoid ripping the <input type="range"> out from under the player's finger on cosmetic updates (chat/props).
      c.deferredPaint = true;
    } else {
      c.draggingRaise = false;
      c.deferredPaint = false;
      paintTable(c);
    }
    if (preOp) executePreAction(c, t, preOp);
    else if (c.preAction && t.hand?.current_player === t.me.player) {
      // Pre-action could not be fulfilled (e.g. opponent raised past check_only / call_exact); clear & alert.
      c.preAction = null;
      haptic.notify('warning');
      paintTable(c);
    }
    const openResult = $('.gc-sheet [data-gc-result-hand]');
    if (openResult && Number(openResult.dataset.gcResultHand) === t.last_hand?.number
      && previous?.last_hand?.revealed?.length !== t.last_hand?.revealed?.length) {
      resultSheet(c, t.last_hand);
    }
    if (t.phase === 'closed') { stopStream(c); streamStatus(c, 'paused'); }
    const me = t.seats.find(s => s.player === t.me.player);
    const notice = t.me.notice;
    if (notice && notice !== c.notice && me?.auto && notice.includes('超时')) {
      c.notice = notice;
      timeoutSheet(c);
    } else if (notice && notice !== c.notice) { c.notice = notice; toast(notice, true); }
    if (!notice) c.notice = '';
  }
  // Finished hands stay on the felt for ~5s (or show a 5s settlement sheet) with optional card reveal.
  const FX_MS = 1800, CELEBRATE_MS = 5000, ZJH_FELT_MS = 1800, RESULT_SHOW_MS = 5000;
  function resolvePreAction(c, t) {
    const pre = c.preAction;
    if (!pre || t.game !== 'holdem' || !t.hand || busy) return null;
    if (t.hand.current_player !== t.me.player) return null;
    const me = t.seats.find(s => s.player === t.me.player);
    if (!me || me.auto || me.leave_after) { c.preAction = null; return null; }
    const legal = t.hand.legal_actions || {};
    if (pre.kind === 'check_fold') return legal.check ? 'check' : legal.fold ? 'fold' : null;
    if (pre.kind === 'check_only') return legal.check ? 'check' : null;
    if (pre.kind === 'call_exact') {
      if (legal.check && pre.amount === 0) return 'check';
      if (legal.call && legal.call.amount <= pre.amount) return 'call';
    }
    return null;
  }
  function executePreAction(c, t, op) {
    c.preAction = null;
    const snapHand = t.hand.number, snapTurn = t.hand.turn;
    setTimeout(() => {
      if (!live(c) || c.table.hand?.number !== snapHand || c.table.hand?.turn !== snapTurn || busy) return;
      haptic.impact('light');
      action(c, op);
    }, 90);
  }
  function react(c, previous, t, suppressTurnAlert = false) {
    const ph = previous?.hand, fx = events(previous, t);
    const loud = ['allin', 'raise', 'chip', 'fold', 'check', 'look'].find(k => fx.some(e => e.kind === k));
    if (loud) sound.play({ look: 'flip', chip: 'chip' }[loud] || loud);
    let dealDelayMs = loud ? 180 : 0;
    if (t.hand && t.hand.number !== ph?.number) {
      clearRunoutTimers(c);
      c.runout = null;
      c.celebrate = null;
      c.boardAnimFrom = 0;
      c.dealAt = performance.now();
      const fresh = previous || (t.server_time - (t.hand.created_at || t.server_time) < 4);
      if (fresh && !isBoard(t.game)) {
        sound.burst('deal', t.game === 'zjh' ? 3 : 2, 105);
        dealDelayMs = 260;
      }
    } else if (t.game === 'holdem' && t.hand && ph && t.hand.number === ph.number) {
      const prevBoard = ph.engine?.board?.length || 0, nextBoard = t.hand.engine?.board?.length || 0;
      if (nextBoard > prevBoard) {
        c.boardAnimFrom = prevBoard;
        c.boardDealAt = performance.now();
        sound.burst('deal', nextBoard - prevBoard, 115);
        dealDelayMs = nextBoard - prevBoard >= 3 ? 380 : 220;
      }
    }
    const ixList = t.interactions || [];
    if (c.lastInteractSeq == null) {
      c.lastInteractSeq = ixList.reduce((m, x) => Math.max(m, x.seq || 0), 0);
    } else {
      for (const ix of ixList) {
        if ((ix.seq || 0) <= c.lastInteractSeq) continue;
        c.lastInteractSeq = ix.seq;
        if (ix.kind === 'prop') {
          sound.play(ix.sfx || 'tap');
          fx.push({ kind: 'prop', item: ix.item, emoji: ix.emoji, label: ix.label, player: ix.actor, target: ix.target, actorName: ix.actor_name, targetName: ix.target_name, ms: 2400 });
        } else if (ix.kind === 'phrase') {
          sound.play('chat', .85);
          fx.push({ kind: 'phrase', player: ix.actor, label: ix.text, ms: 3600 });
        } else if (ix.kind === 'reveal') {
          sound.play('flip');
          fx.push({ kind: 'reveal', player: ix.actor, label: `🃏 亮牌${ix.hand_name ? ' · ' + ix.hand_name : ''}`, ms: 3200 });
        }
      }
    }
    if (t.hand?.turn !== ph?.turn || t.hand?.number !== ph?.number) { c.sel = null; c.preview = null; }
    if (isBoard(t.game) && ph) {
      const h = t.hand?.number === ph.number ? t.hand : t.last_hand?.number === ph.number ? t.last_hand : null;
      if ((h?.moves?.length || 0) > (ph.moves?.length || 0)) {
        const fewer = t.game === 'xiangqi' && Object.keys(xqParse(h.board?.fen)).length < Object.keys(xqParse(ph.board?.fen)).length;
        sound.play(fewer ? 'check' : 'flip');
        if (t.game === 'xiangqi' && h.in_check && h.last_move) fx.push({ player: h.last_move.player, kind: 'raise', label: '将军' });
      }
    }
    const meSeatObj = t.seats.find(s => s.player === t.me.player);
    if (!suppressTurnAlert && !meSeatObj?.auto && t.me.player && t.hand?.current_player === t.me.player
      && (ph?.current_player !== t.me.player || ph?.number !== t.hand.number || ph?.turn !== t.hand.turn && !ph?.current_player)) {
      c.turnAlertAt = performance.now();
      clearTimeout(c.turnAlertTimer);
      const snapHand = t.hand.number, snapTurn = t.hand.turn;
      const fireTurnCue = () => {
        if (!live(c) || c.table.hand?.number !== snapHand || c.table.hand?.turn !== snapTurn) return;
        if (c.table.hand?.current_player !== c.table.me.player) return;
        sound.play('turn', 1);
        haptic.turnAlert();
      };
      if (dealDelayMs > 0) c.turnAlertTimer = setTimeout(fireTurnCue, dealDelayMs);
      else fireTurnCue();
    }
    if (ph && t.last_hand?.number === ph.number && t.hand?.number !== ph.number) {
      const h = t.last_hand, net = h.result?.net || {}, motion = !matchMedia('(prefers-reduced-motion: reduce)').matches;
      const feltMs = t.game === 'zjh' ? ZJH_FELT_MS : CELEBRATE_MS;
      clearTimeout(c.celebrateTimer);
      clearRunoutTimers(c);
      c.runout = null;
      const totalBoard = h.engine?.board?.length || 0;
      const runFrom = t.game === 'holdem' && h.runout_from != null && h.runout_from < totalBoard ? h.runout_from : null;
      const startCelebrate = () => {
        if (!live(c) || c.table.hand) return;
        c.runout = null;
        if (motion) {
          c.celebrate = { hand: h, at: performance.now(), ms: feltMs };
          pushFx(c, Object.entries(net).filter(([, n]) => n).map(([player, n]) =>
            ({ player, kind: n > 0 ? 'win' : 'loss', label: signed(n), ms: CELEBRATE_MS })));
        }
        if (net[t.me.player] > 0) { sound.play('win'); haptic.notify('success'); }
        else if (net[t.me.player] < 0) { sound.play('lose'); haptic.notify('error'); }
        paintTable(c);
        c.celebrateTimer = setTimeout(() => {
          if (!live(c) || motion && c.celebrate?.hand !== h) return;
          if (motion) { c.celebrate = null; paintTable(c); }
          const me = c.table.seats.find(s => s.player === c.table.me.player);
          if (c.table.game === 'zjh' && !c.table.hand && !me?.auto && !$('.gc-sheet')) resultSheet(c, c.table.last_hand || h, null, motion);
        }, motion ? feltMs : 0);
      };
      if (motion && runFrom !== null) {
        // Dramatic street-by-street All-in runout reveal before winner banner.
        const stages = [];
        if (runFrom < 3 && totalBoard >= 3) stages.push(3);
        if (runFrom < 4 && totalBoard >= 4) stages.push(4);
        if (runFrom < 5 && totalBoard >= 5) stages.push(5);
        c.runout = { hand: h, shownCount: runFrom };
        haptic.impact('heavy');
        let delay = 150;
        stages.forEach((cnt, idx) => {
          const prevCnt = idx === 0 ? runFrom : stages[idx - 1];
          c.runoutTimers.push(setTimeout(() => {
            if (!live(c) || c.table.hand) return;
            c.runout = { hand: h, shownCount: cnt };
            c.boardAnimFrom = prevCnt;
            c.boardDealAt = performance.now();
            sound.burst('deal', cnt - prevCnt, 90);
            haptic.impact(cnt === 5 ? 'medium' : 'light');
            paintTable(c);
          }, delay));
          delay += cnt === 3 ? 580 : 560;
        });
        c.runoutTimers.push(setTimeout(startCelebrate, delay + 180));
      } else {
        if (motion) {
          c.celebrate = { hand: h, at: performance.now(), ms: feltMs };
          fx.push(...Object.entries(net).filter(([, n]) => n).map(([player, n]) =>
            ({ player, kind: n > 0 ? 'win' : 'loss', label: signed(n), ms: CELEBRATE_MS })));
        }
        if (net[t.me.player] > 0) { sound.play('win'); haptic.notify('success'); }
        else if (net[t.me.player] < 0) { sound.play('lose'); haptic.notify('error'); }
        c.celebrateTimer = setTimeout(() => {
          if (!live(c) || motion && c.celebrate?.hand !== h) return;
          if (motion) { c.celebrate = null; paintTable(c); }
          const me = c.table.seats.find(s => s.player === c.table.me.player);
          if (c.table.game === 'zjh' && !c.table.hand && !me?.auto && !$('.gc-sheet')) resultSheet(c, c.table.last_hand || h, null, motion);
        }, motion ? feltMs : 0);
      }
    }
    if (fx.length) pushFx(c, fx);
  }
  const celebrating = c => !c.table.hand && c.celebrate && performance.now() - c.celebrate.at < (c.celebrate.ms || CELEBRATE_MS) ? (c.table.last_hand || c.celebrate.hand) : null;
  // The server sends state, not an action log, so table effects are derived from consecutive snapshots.
  function events(prev, t) {
    const ph = prev?.hand;
    if (!ph) return [];
    const h = t.hand?.number === ph.number ? t.hand : t.last_hand?.number === ph.number ? t.last_hand : null;
    if (!h) return [];
    const before = new Map(handPlayers(prev, ph).map(p => [p.player, p]));
    const after = handPlayers(t, h);
    const actor = ph.current_player, out = [];
    const grew = p => (p.invested || 0) - (before.get(p.player)?.invested || 0);
    const actorGrew = grew(after.find(p => p.player === actor) || {});
    for (const p of after) {
      const b = before.get(p.player), delta = grew(p);
      if (!b) continue;
      if (t.game === 'zjh' && p.seen && !b.seen) out.push({ player: p.player, kind: 'look', label: '看牌' });
      if (p.folded && !b.folded) {
        const compared = t.game === 'zjh' && actorGrew > 0;
        out.push({ player: p.player, kind: 'fold', label: compared ? '比牌输' : '弃牌' });
        if (compared && p.player !== actor) out.push({ player: actor, kind: 'chip', label: '比牌', amount: actorGrew });
      } else if (p.all_in && !b.all_in) {
        out.push({ player: p.player, kind: 'allin', label: t.game === 'zjh' ? '孤注一掷' : '全下', amount: delta });
      } else if (delta > 0 && !(t.game === 'zjh' && out.some(e => e.label === '比牌' && e.player === p.player))) {
        const raised = t.game === 'zjh' ? h.bet > ph.bet
          : h.engine?.street === ph.engine?.street && (h.engine?.current_bet || 0) > (ph.engine?.current_bet || 0);
        const label = raised ? t.game === 'holdem' && !ph.engine?.current_bet ? '下注' : '加注' : '跟注';
        out.push({ player: p.player, kind: raised ? 'raise' : 'chip', label, amount: delta });
      }
    }
    const acted = out.some(e => e.player === actor);
    const moved = h.current_player !== actor || h.engine?.street !== ph.engine?.street || h !== t.hand;
    if (t.game === 'holdem' && actor && !acted && moved && before.has(actor)) out.push({ player: actor, kind: 'check', label: '过牌' });
    return out;
  }
  function pushFx(c, list) {
    const at = performance.now();
    c.fx = [...(c.fx || []).filter(f => at - f.at < (f.ms || FX_MS)), ...list.map(f => ({ ...f, at }))];
  }
  function seatPoint(c, player) {
    const root = $('.gc-table');
    const ring = $(`.gc-table [data-gc-player="${CSS.escape(player)}"] .gc-avatar-ring`)
      || (player === c.table.me.player ? $('.gc-me-row .gc-avatar-ring') : null);
    if (!root || !ring) return null;
    const r = ring.getBoundingClientRect(), o = root.getBoundingClientRect();
    return { x: r.left - o.left + r.width / 2, y: r.top - o.top + r.height / 2 };
  }
  function drawFx(c) {
    const root = $('.gc-table');
    if (!root || !c.fx?.length) return;
    const at = performance.now(), o = root.getBoundingClientRect();
    const potBox = $('.gc-pot-value, .gc-celebrate-crown, .gc-wait-center > b', root)?.getBoundingClientRect();
    const pot = potBox && { x: potBox.left - o.left + potBox.width / 2, y: potBox.top - o.top + potBox.height / 2 };
    const layer = document.createElement('div');
    layer.className = 'gc-fx-layer';
    layer.innerHTML = c.fx.map(f => {
      const age = at - f.at;
      if (age >= (f.ms || FX_MS)) return '';
      const delay = `animation-delay:-${Math.round(age)}ms`;
      if (f.kind === 'prop') {
        const from = seatPoint(c, f.player) || { x: o.width / 2, y: o.height - 90 };
        const to = seatPoint(c, f.target);
        if (!to) return '';
        const ringEl = $(`.gc-table [data-gc-player="${CSS.escape(f.target)}"] .gc-avatar-ring`)
          || (f.target === c.table.me.player ? $('.gc-me-row .gc-avatar-ring') : null);
        if (ringEl && age > 420 && age < 1600 && (f.item === 'bomb' || f.item === 'poop')) {
          ringEl.classList.add('gc-hit-shake');
        }
        return `<span class="gc-fx-proj gc-proj-${esc(f.item)}" style="left:${from.x}px;top:${from.y}px;--dx:${to.x - from.x}px;--dy:${to.y - from.y}px;${delay}">${esc(f.emoji)}</span>`
          + `<div class="gc-fx-impact gc-impact-${esc(f.item)}" style="left:${to.x}px;top:${to.y}px;animation-delay:${Math.round(430 - age)}ms"><span>${esc(f.emoji)}</span><i></i></div>`
          + `<div class="gc-fx-banner" style="${delay}">${esc(f.emoji)} <b>${esc(f.actorName)}</b> 向 <b>${esc(f.targetName)}</b> ${esc(f.item === 'flower' ? '献上鲜花' : f.item === 'champagne' ? '敬了香槟' : '扔了' + f.label)}</div>`;
      }
      const spot = seatPoint(c, f.player);
      if (!spot) return '';
      if (f.kind === 'phrase') {
        const left = Math.max(82, Math.min(o.width - 82, spot.x));
        return `<div class="gc-fx-speech" style="left:${left}px;top:${Math.max(46, spot.y - 42)}px;${delay}">💬 ${esc(f.label)}</div>`;
      }
      const bubble = `<div class="gc-fx-bubble gc-fx-${f.kind}" style="left:${spot.x}px;top:${spot.y - 34}px;${delay}">${esc(f.label)}${f.amount ? ' ' + num(f.amount) : ''}</div>`;
      const chip = pot && ['chip', 'raise', 'allin'].includes(f.kind) ? `<span class="gc-fx-chip" style="left:${spot.x}px;top:${spot.y}px;--dx:${pot.x - spot.x}px;--dy:${pot.y - spot.y}px;${delay}">${img('chip')}</span>` : '';
      return bubble + chip;
    }).join('');
    root.append(layer);
  }
  function tagOf(seat, t) {
    // Public profile tags are optional API fields; never invent another member's level/title.
    return seat.level_tag || (seat.player === t.me.player ? myTag() : null);
  }
  function avatar(seat, t, cls = '') {
    const tag = tagOf(seat, t);
    return `
      <div class="avatar ${cls} ${esc(lvClass(tag))} ${seat.bot ? 'gc-bot' : ''}">${seat.bot ? '🤖' : esc(Array.from(seat.name || '?')[0])}${seat.avatar
        ? `<img src="${esc(seat.avatar)}" alt="" loading="lazy" onerror="this.remove()">` : ''}</div>`;
  }
  function label(seat, t) {
    return seat.bot ? '<span class="gc-bot-label">机器人</span>' : lvTag(tagOf(seat, t), true);
  }
  function titlePlaque(seat, t) {
    const profile = seat.player === t.me.player ? getProfile() : null;
    const title = seat.title || profile?.worn_title || profile?.level?.title;
    const tag = tagOf(seat, t);
    return typeof title === 'string' && title ? `
      <div class="gc-plaque ${esc(lvClass(tag))}">${plaque(title, tag, false, profile?.level || tag)}</div>` : '';
  }
  const ranks = ['2','3','4','5','6','7','8','9','10','J','Q','K','A'];
  function card(code, size = '', best = [], extraCls = '') {
    if (!Number.isInteger(code) || code < 0 || code > 51) return `<span class="gc-card gc-card-back ${size} ${extraCls}" aria-label="未公开的牌">${img('card-back')}</span>`;
    const s = Math.floor(code / 13), suit = ['♠', '♥', '♣', '♦'][s], rank = ranks[code % 13];
    const isBest = best.length > 0 && best.includes(code);
    const dim = best.length >= 5 && !isBest ? 'gc-card-dim' : '';
    return `<span class="gc-card ${s === 1 || s === 3 ? 'red' : ''} ${size} ${isBest ? 'gc-best' : ''} ${dim} ${extraCls}" aria-label="${['黑桃','红桃','梅花','方块'][s]}${rank}"><span class="gc-card-rank">${rank}<i>${suit}</i></span><span class="gc-card-suit">${suit}</span></span>`;
  }
  function cards(values, count, size = '', best = []) { return Array.from({ length: count }, (_, i) => card(values?.[i], size, best)).join(''); }
  const handPlayers = (t, h = t.hand) => h ? t.game === 'holdem' ? h.engine?.players || [] : h.players || [] : [];
  function seatStatus(seat, t, p) {
    if (seat.leave_after) return '本手后离桌';
    if (seat.away) return seat.away_since == null && p ? '下手暂离' : '暂离中';
    if (seat.auto) return '托管中';
    if (p?.folded) return '已弃牌';
    if (p?.all_in) return '已全下';
    if (t.hand?.current_player === seat.player) return '思考中';
    if (t.phase === 'playing') return p ? t.game === 'zjh' ? p.seen ? '已看牌' : '闷牌中' : '在局' : '下手加入';
    if (t.game === 'holdem') return seat.stack > 0 ? '已入座' : '需补码';
    return seat.ready ? '已准备' : '未准备';
  }
  function markers(seat, t) {
    if (t.game !== 'holdem') return '';
    const e = t.hand?.engine || { button: t.button };
    return `${e.button === seat.seat ? img('dealer-button', 'gc-dealer', '庄家 D') : ''}${e.small_blind_seat === seat.seat ? '<span class="gc-blind">小盲</span>' : e.big_blind_seat === seat.seat ? '<span class="gc-blind bb">大盲</span>' : ''}`;
  }
  function seatActionBadge(seat, t, hand, p) {
    if (t.game !== 'holdem' || !hand || !p) return null;
    if (seat.leave_after || seat.away || seat.auto) return null;
    if (t.hand?.current_player === seat.player) return null;
    const last = hand.last_actions?.[seat.player];
    if (!last) return null;
    const act = last.all_in ? 'all_in' : last.action;
    const zh = HOLDEM_ACTION_ZH[act];
    if (!zh) return null;
    return { text: zh, cls: `gc-act-${act}` };
  }
  function seatHtml(seat, pos, t, hand = t.hand, highlightBest = []) {
    if (!seat) return `
      <div class="gc-seat gc-pos${pos} gc-empty-seat">
        <div class="gc-avatar-ring">＋</div>
        <span>空位</span>
      </div>`;
    const p = handPlayers(t, hand).find(p => p.player === seat.player);
    const active = t.hand?.current_player === seat.player;
    const won = !t.hand && hand?.result?.winners?.includes(seat.player);
    const canInteract = !!t.me.player && seat.player !== t.me.player && t.phase !== 'closed';
    const actBadge = seatActionBadge(seat, t, hand, p);
    const statusText = actBadge ? actBadge.text : seatStatus(seat, t, p);
    const statusCls = seat.auto ? 'gc-auto-status' : seat.ready ? 'gc-ready-status' : actBadge ? actBadge.cls : '';
    const streetBet = t.game === 'holdem' ? (p?.bet || 0) : (p?.invested ?? p?.bet ?? 0);
    const showBet = t.game === 'holdem' ? streetBet > 0 : !!p;
    const showBest = won ? highlightBest : [];
    const stackVal = fmtChips(p?.stack ?? seat.stack, t);
    const hasDealer = t.game === 'holdem' && (t.hand?.engine?.button ?? t.button) === seat.seat;
    return `
      <div class="gc-seat gc-pos${pos} ${p?.folded ? 'gc-folded' : ''} ${won ? 'gc-winner' : ''} ${p?.all_in ? 'gc-allin-seat' : ''} ${hasDealer ? 'gc-has-dealer' : ''}" data-gc-player="${esc(seat.player)}">
        <div class="gc-avatar-ring ${active ? 'gc-active' : ''} ${canInteract ? 'gc-interactable' : ''}" ${active ? 'data-gc-ring' : ''} ${canInteract ? `data-gc-interact-seat="${esc(seat.player)}" title="点击互动"` : ''}>
          ${avatar(seat, t)}${markers(seat, t)}${canInteract ? '<i class="gc-gift-dot" aria-hidden="true">🎁</i>' : ''}
          <span class="gc-seat-status ${statusCls}">${statusText}</span>
        </div>
        <div class="gc-seat-name">${esc(seat.name)}</div>
        <div class="gc-seat-tags">${seat.player === t.host ? '<span class="gc-host">房主</span>' : ''}${label(seat, t)}</div>
        ${titlePlaque(seat, t)}${t.game === 'holdem' ? `
          <div class="gc-stack num" data-gc-bb-toggle title="点击切换筹码 / BB">${stackVal}</div>` : ''}${p ? `
          <div class="gc-mini-cards">${cards(p.cards, t.game === 'zjh' ? 3 : 2, 'xs', showBest)}</div>
          ${showBet ? `<span class="gc-bet num">${img('chip')} ${fmtChips(streetBet, t)}</span>` : ''}` : ''}
      </div>`;
  }
  function startState(t) {
    const host = t.seats.find(s => s.player === t.host);
    const need = t.rules.min_balance ?? t.rules.cap ?? 0;
    const enough = funds(t.me.wallet, t) >= need;
    const ss = t.start_status || {};
    const eligible = ss.eligible_players ?? (t.game === 'zjh'
      ? t.seats.filter(s => (s.ready || s.player === t.host) && (s.player !== t.me.player || enough)).length
      : t.seats.filter(s => !s.away && s.stack > 0).length);
    const missing = Math.max(0, 2 - eligible), empty = Math.max(0, 2 - t.seats.length);
    let reason = empty ? `还差 ${empty} 人入座才能开局，可以点「群播报说明」分享桌链接`
      : missing ? t.game === 'zjh' ? `等待 ${missing} 位玩家点准备` : `等待 ${missing} 位玩家带入筹码`
      : t.me.is_host ? '人数够了，点「开始」就能开局' : `人数够了，等房主 ${host?.name || ''} 开始`;
    if ((t.game === 'zjh' || isBoard(t.game)) && ss.message) reason = ss.message;
    if (t.game === 'holdem' && ss.reason) reason = {
      paused_for_players: '发牌已暂停：至少 2 人有筹码且没暂离才会继续',
      practice_player_unavailable: '练习桌已暂停；恢复入局或补码后继续',
      next_hand: '等待下一手自动发牌', closed: '本桌已结束',
    }[ss.reason] || reason;
    if (t.game === 'zjh' && t.me.is_host && !enough) reason = `你需 ${num(need)} 可用${unit(t)}才能参与；${eligible >= 2 ? '其他人已够开局，你本手旁观' : '人数还不够开局'}`;
    const can = t.game !== 'holdem' && ss.can_start != null ? ss.can_start : t.me.is_host && (ss.can_start ?? eligible >= 2);
    return { eligible, enough, can, reason, host };
  }
  function croupier(t) {
    const tier = Object.hasOwn(tierClasses, t.tier) ? t.tier : 'practice';
    return `
      <div class="gc-croupier" aria-hidden="true"><span class="gc-croupier-silhouette"></span><img src="/app/assets/game-assets/dealer-${tier}.webp" alt="" onload="this.parentElement.classList.add('has-art')" onerror="this.remove()"></div>`;
  }
  function boardCardsHtml(board, best, animFrom, freshBoard) {
    return Array.from({ length: 5 }, (_, i) => {
      if (i >= (board || []).length) return '<span class="gc-card-slot"></span>';
      const flipCls = freshBoard && i >= animFrom ? 'gc-card-flip' : '';
      return card(board[i], 'sm', best, flipCls);
    }).join('');
  }
  function celebration(t, h, highlightBest = []) {
    const winners = h.result?.winners || [];
    const who = winners.map(id => t.seats.find(s => s.player === id)?.name || '已离桌玩家').join('、');
    const won = Math.max(0, ...winners.map(id => h.result?.net?.[id] || 0));
    const isShowdown = t.game === 'holdem' && Object.values(h.result?.players || {}).some(r => r.shown);
    const winHandName = isShowdown ? winners.map(id => h.result?.players?.[id]?.hand).find(Boolean) : '';
    const why = t.game === 'holdem'
      ? (isShowdown ? (winHandName ? `摊牌 · ${winHandName}` : '摊牌比大小') : '其他人都弃牌了')
      : { last_player: '其他人都出局了', all_in: '孤注一掷 · 全员比牌', round_limit: '满 10 轮 · 统一比牌' }[h.reason] || '比牌结束';
    const board = t.game === 'holdem' ? `<div class="gc-board">${(h.engine?.board || []).map(code => card(code, 'sm', highlightBest)).join('')}</div>` : '';
    const revealBtn = h.can_reveal ? `<button class="btn sm gc-reveal-btn" type="button" data-gc-reveal="${h.number}">🃏 亮牌</button>`
      : (t.me.player && h.revealed?.includes(t.me.player) ? '<span class="gc-revealed-pill">🃏 已亮牌</span>' : '');
    return `
      <div class="${t.game === 'holdem' ? 'gc-holdem-center' : 'gc-pot'} gc-celebrate">${board}
        ${img('crown', 'gc-celebrate-crown')}
        <div class="gc-celebrate-name">${esc(who || '本手')} 赢了</div>
        <div class="gc-pot-value num">+${fmtChips(won, t)}</div>
        <div class="small">${esc(why)}</div>
        <div class="gc-celebrate-actions">${revealBtn}<button class="gc-tool sm" type="button" data-gc-history>结算单</button></div>
      </div>`;
  }
  function tableCenter(c, t, shown = null, highlightBest = []) {
    if (!t.hand && c.runout && t.game === 'holdem') {
      const h = c.runout.hand, e = h.engine || {};
      const visibleBoard = (e.board || []).slice(0, c.runout.shownCount);
      const streetLbl = visibleBoard.length >= 5 ? '河牌 · 决胜' : visibleBoard.length === 4 ? '转牌 · 跑马' : visibleBoard.length >= 3 ? '翻牌 · 跑马' : '全下摊牌';
      const freshBoard = c.boardDealAt && (performance.now() - c.boardDealAt < 700);
      return `
        <div class="gc-holdem-center gc-runout-center">
          <div class="gc-street-pill gc-runout-pill">🔥 全下跑马 · ${streetLbl}</div>
          <div class="gc-board">${boardCardsHtml(visibleBoard, [], c.boardAnimFrom || 0, freshBoard)}</div>
          <div class="gc-pot-value num">${img('chip-stack')} ${fmtChips(e.pot, t)}</div>
          <div class="gc-pots">${(e.pots || []).map((p, i) => `<span>${i ? '边池 ' + i : '主池'} ${fmtChips(p.amount, t)}</span>`).join('')}</div>
        </div>`;
    }
    if (shown) return celebration(t, shown, highlightBest);
    if (!t.hand) {
      const s = startState(t);
      return `
        <div class="gc-pot gc-wait-center">
          <div class="tiny muted">${t.phase === 'closed' ? '本桌已结束' : t.phase === 'between' ? '准备下一手'
            : t.start_status?.reason === 'paused_for_players' ? '发牌已暂停' : '等待开局'}</div>
          <b>${t.seats.length}<small>/${t.max_players}</small></b>
          <div class="small">${t.countdown_at ? '<span data-gc-countdown>5</span> 秒后自动开局' : t.phase === 'closed' ? '积分已按结算退回' : `2 人即可开局 · 最多 ${t.max_players} 人`}</div>
          <div class="tiny muted">${t.phase !== 'closed' ? '房主 ' + esc(s.host?.name || '—') : ''}</div>
          <div class="tiny gc-idle" data-gc-idle></div>
        </div>`;
    }
    const h = t.hand;
    if (t.game === 'holdem') {
      const e = h.engine || {};
      const freshBoard = c.boardDealAt && (performance.now() - c.boardDealAt < 700);
      const actorName = t.seats.find(s => s.player === h.current_player)?.name;
      const isMeActing = !!(t.me.player && h.current_player === t.me.player);
      return `
        <div class="gc-holdem-center">
          <div class="gc-street-pill">第 ${h.number} 手 · ${esc(STREET_ZH[e.street] || '进行中')}</div>
          <div class="gc-board">${boardCardsHtml(e.board || [], highlightBest, c.boardAnimFrom || 0, freshBoard)}</div>
          <button class="gc-pot-value gc-pot-btn num" type="button" data-gc-bb-toggle title="点击切换筹码 / 大盲(BB)显示">${img('chip-stack')} ${fmtChips(e.pot, t)}</button>
          <div class="gc-pots">${(e.pots || []).map((p, i) => `<span>${i ? '边池 ' + i : '主池'} ${fmtChips(p.amount, t)}</span>`).join('')}</div>
          <span class="gc-ticker ${isMeActing ? 'gc-ticker-me' : ''}">${isMeActing ? '⚡ 轮到你行动' : actorName ? esc(actorName) + ' 行动中' : '等待服务端发牌'}</span>
        </div>`;
    }
    return `
      <div class="gc-pot">
        <div class="gc-pot-label">底池 · ${unit(t)}</div>
        ${img('chip-stack', 'gc-pot-art')}
        <div class="gc-pot-value num">${num(h.pot)}</div>
        <div class="small">第 ${num(h.round)} / 10 轮 · 当前注 ${num(h.bet)}</div>
        <span class="gc-ticker">${esc(t.seats.find(s => s.player === h.current_player)?.name || '玩家')} 行动中</span>
      </div>`;
  }
  function holdemPresets(t, h, legal) {
    const e = h.engine || {}, bb = Math.max(1, t.rules?.big_blind || 1);
    const potAfterCall = (e.pot || 0) + (h.legal_actions?.call?.amount || 0);
    const curBet = e.current_bet || 0;
    const clamp = v => Math.max(legal.min, Math.min(legal.max, Math.round(v)));
    const isUnopenedPreflop = e.street === 'preflop' && curBet <= bb;
    const candidates = isUnopenedPreflop
      ? [
          { label: '2BB', val: clamp(bb * 2), sizeKey: '2bb' },
          { label: '2.5BB', val: clamp(bb * 2.5), sizeKey: '2.5bb' },
          { label: '3BB', val: clamp(bb * 3), sizeKey: '3bb' },
          { label: '½ 池', val: clamp(curBet + potAfterCall * 0.5), sizeKey: '0.5', keep: true },
          { label: '全下', val: legal.max, sizeKey: '0', keep: true },
        ]
      : [
          { label: '⅓ 池', val: clamp(curBet + potAfterCall / 3), sizeKey: '0.33' },
          { label: '½ 池', val: clamp(curBet + potAfterCall * 0.5), sizeKey: '0.5', keep: true },
          { label: '¾ 池', val: clamp(curBet + potAfterCall * 0.75), sizeKey: '0.75' },
          { label: '1 池', val: clamp(curBet + potAfterCall), sizeKey: '1' },
          { label: '全下', val: legal.max, sizeKey: '0', keep: true },
        ];
    const seen = new Set();
    return candidates.filter(p => {
      if (p.keep) { seen.add(p.val); return true; }
      if (seen.has(p.val)) return false;
      seen.add(p.val);
      return true;
    });
  }
  function inlineRaiseHtml(c, t) {
    const rb = c.raiseBar, h = t.hand;
    if (!rb || !h || t.game !== 'holdem') return '';
    const legal = h.legal_actions?.[rb.op];
    if (!legal || legal.min == null) return '';
    const val = Math.max(legal.min, Math.min(legal.max, Math.round(rb.value ?? legal.min)));
    rb.value = val;
    const bb = Math.max(1, t.rules?.big_blind || 1);
    const bbText = (val / bb).toFixed(1).replace(/\.0$/, '');
    const presets = holdemPresets(t, h, legal);
    const isAllIn = val >= legal.max;
    const mySeat = handPlayers(t, h).find(x => x.player === t.me.player);
    const mySeatCards = t.game === 'holdem' && c.raiseBar && mySeat?.cards?.length
      ? `<span class="gc-raise-hole">${mySeat.cards.map(code => {
          if (!Number.isInteger(code)) return '';
          const s = Math.floor(code / 13), suit = ['♠', '♥', '♣', '♦'][s], rank = ranks[code % 13];
          return `<em class="${s === 1 || s === 3 ? 'red' : ''}">${rank}${suit}</em>`;
        }).join('')}</span>`
      : '';
    return `
      <div class="gc-raise-inline" data-gc-raise-inline>
        <div class="gc-raise-head">
          <span class="gc-raise-title">${rb.op === 'raise' ? '加注到' : '下注到'}</span>
          <b class="gc-raise-num num" data-gc-raise-val>${num(val)} <small>(${bbText} BB)</small></b>
          ${mySeatCards}
          <button class="gc-raise-close" type="button" data-gc-raise-cancel aria-label="收起加注条">✕</button>
        </div>
        <div class="gc-raise-presets">${presets.map(pr => `
          <button class="gc-preset-btn ${val === pr.val ? 'on' : ''}" type="button" data-gc-size="${pr.sizeKey}" data-gc-preset-val="${pr.val}">${esc(pr.label)}</button>`).join('')}
        </div>
        <div class="gc-raise-slider-row">
          <button class="gc-step-btn" type="button" data-gc-raise-step="${-bb}" aria-label="减少 1 BB">−</button>
          <input class="gc-range" type="range" aria-label="加注到" min="${legal.min}" max="${legal.max}" step="1" value="${val}">
          <button class="gc-step-btn" type="button" data-gc-raise-step="${bb}" aria-label="增加 1 BB">＋</button>
          <button class="gc-raise-go ${isAllIn ? 'allin' : ''}" type="button" data-gc-raise-confirm ${disabled(false)}>${isAllIn ? '全下' : rb.op === 'raise' ? '确认加注' : '确认下注'}</button>
        </div>
      </div>`;
  }
  function preActionHtml(c, t, me, player) {
    if (t.game !== 'holdem' || !t.hand || !player || player.folded || player.all_in || me.auto || me.leave_after) return '';
    const e = t.hand.engine || {};
    const toCall = Math.max(0, (e.current_bet || 0) - (player.bet || 0));
    const callCap = Math.min(toCall, player.stack || 0);
    const pre = c.preAction;
    const isSel = kind => pre && pre.handNo === t.hand.number && pre.street === e.street && pre.kind === kind;
    return `
      <div class="gc-preactions" role="group" aria-label="预选行动">
        <button class="gc-pre-btn ${isSel('check_fold') ? 'on' : ''}" type="button" data-gc-pre="check_fold"><i></i>过/弃</button>
        <button class="gc-pre-btn ${isSel('check_only') ? 'on' : ''}" type="button" data-gc-pre="check_only" ${toCall > 0 ? 'disabled' : ''}><i></i>自动过牌</button>
        <button class="gc-pre-btn ${isSel('call_exact') ? 'on' : ''}" type="button" data-gc-pre="call_exact" data-gc-pre-amt="${callCap}"><i></i>${callCap > 0 ? (callCap >= player.stack ? '跟注全下' : '跟注 ' + fmtChips(callCap, t)) : '过牌/跟0'}</button>
      </div>`;
  }
  function controls(c, t, me) {
    if (t.phase === 'closed') return '<button class="btn block" data-gc-back>返回游戏中心</button>';
    if (!me) return `
      <div class="gc-note">你正在看桌，尚未入座，不会扣分。${t.phase === 'playing' ? '现在入座从下一手参与。' : ''}</div>
      <button class="btn block" data-gc-seat ${disabled(t.seats.length >= t.max_players)}>${t.seats.length >= t.max_players ? '本桌已满' : t.game === 'holdem' ? '选择带入并入座' : '入座 · 不扣分'}</button>`;
    if (me.leave_after) return '<div class="gc-wait-bar">已安排本手结束后离桌，等待服务端结算</div>';
    if (!t.hand) {
      const state = startState(t);
      if (t.game === 'holdem' && (t.phase === 'between' || t.hand_no > 0)) return `
        <div class="gc-wait-bar">${me.away ? '暂离中 · 下一手跳过你，不扣盲注'
          : !me.stack ? '筹码已用完，请补码或离桌'
          : t.countdown_at ? '<span data-gc-countdown>5</span> 秒后继续发牌' : esc(state.reason)}${t.last_hand?.can_reveal ? ` <button class="btn sm gc-reveal-btn" type="button" data-gc-reveal="${t.last_hand.number}">🃏 亮牌</button>` : ''}</div>`;
      return `
        <div class="gc-wait-reason">${t.countdown_at ? '全员已准备 · <b data-gc-countdown>5</b> 秒后开局' : esc(state.reason)}</div>
        <div class="gc-actions">${t.game !== 'holdem' ? `
          <button class="gc-action ${me.ready ? '' : 'main'}" data-gc-ready ${disabled(!me.ready && !state.enough)}>
            ${me.ready ? '取消准备' : !state.enough ? '余额不足' : t.last_hand && isBoard(t.game) ? '再来一局' : '准备'}
            <small>${me.ready ? t.me.is_host ? '人够了就点开始' : '等待房主开始' : isBoard(t.game) ? '每局需重新准备' : '每手需重新准备'}</small>
          </button>` : ''}${t.me.is_host ? `
          <button class="gc-action gold" data-gc-start ${disabled(!state.can)}>开始
            <small>${state.can ? t.game === 'holdem' ? '有筹码且未暂离者参加' : isBoard(t.game) ? '对手已准备' : '未准备的人本手旁观' : '人数还不够'}</small>
          </button>` : '<div class="gc-wait-bar grow">等待房主开始</div>'}
        </div>`;
    }
    const legal = t.hand.legal_actions || {};
    const actions = Object.keys(legal).filter(a => a !== 'look');
    const player = handPlayers(t).find(p => p.player === me.player);
    const labels = {
      fold: '弃牌', call: t.game === 'holdem' && legal.call?.amount === player?.stack ? '跟注全下' : '跟注',
      check: '过牌', raise: '加注', bet: '下注', compare: '比牌', all_in: t.game === 'zjh' ? '孤注一掷' : '全下',
    };
    const holdemRaiseOpen = t.game === 'holdem' && c.raiseBar && legal[c.raiseBar.op];
    const preHtml = !actions.length ? preActionHtml(c, t, me, player) : '';
    return `
      ${holdemRaiseOpen ? inlineRaiseHtml(c, t) : ''}
      ${actions.length ? `
        <div class="gc-actions">${actions.map(a => `
          <button class="gc-action ${['call', 'check'].includes(a) ? 'main'
            : ['raise', 'bet'].includes(a) ? (holdemRaiseOpen && c.raiseBar.op === a ? 'alt on' : 'alt')
            : ['compare', 'all_in'].includes(a) ? 'warm' : ''}"
            data-gc-action="${esc(a)}" ${disabled(false)}>
            ${esc(labels[a] || a)}
            <small>${legal[a].amount != null ? fmtChips(legal[a].amount, t)
              : legal[a].min != null ? '最小 ' + fmtChips(legal[a].min, t) : a === 'fold' ? '放弃本手' : '按规则执行'}</small>
          </button>`).join('')}</div>` : preHtml || `
        <div class="gc-wait-bar">${me.auto ? '托管正在替你操作，可随时取消' : '等待其他玩家 · 本手未轮到你'}</div>`}
      <div class="gc-turn-line">行动剩余 <b data-gc-seconds>—</b> 秒 · 超时进入托管
        <button class="link" data-gc-rules>了解托管</button>
      </div>`;
  }
  // Board games. Xiangqi uses Pikafish coordinates (files a–i, red on rank 0); Gomoku uses [x, y] from the top-left.
  const XQ_CHARS = { K: '帅', A: '仕', B: '相', N: '马', R: '车', C: '炮', P: '兵', k: '将', a: '士', b: '象', n: '马', r: '车', c: '炮', p: '卒' };
  const XQ_START = 'rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w';
  const XQ_GRID = (() => {
    const X = c => 50 + c * 100, Y = r => 50 + r * 100, d = [];
    for (let r = 0; r < 10; r++) d.push(`M${X(0)} ${Y(r)}H${X(8)}`);
    for (let c = 0; c < 9; c++) d.push(c % 8 ? `M${X(c)} ${Y(0)}V${Y(4)}M${X(c)} ${Y(5)}V${Y(9)}` : `M${X(c)} ${Y(0)}V${Y(9)}`);
    d.push(`M${X(3)} ${Y(0)}L${X(5)} ${Y(2)}M${X(5)} ${Y(0)}L${X(3)} ${Y(2)}M${X(3)} ${Y(7)}L${X(5)} ${Y(9)}M${X(5)} ${Y(7)}L${X(3)} ${Y(9)}`);
    for (const [c, r] of [[1, 2], [7, 2], [1, 7], [7, 7], [0, 3], [2, 3], [4, 3], [6, 3], [8, 3], [0, 6], [2, 6], [4, 6], [6, 6], [8, 6]]) {
      for (const sx of [-1, 1]) for (const sy of [-1, 1]) {
        if (c + sx < 0 || c + sx > 8) continue;
        const x = X(c) + sx * 9, y = Y(r) + sy * 9;
        d.push(`M${x + sx * 20} ${y}H${x}V${y + sy * 20}`);
      }
    }
    return `<svg class="gc-board-lines" viewBox="0 0 900 1000" aria-hidden="true"><rect class="edge" x="38" y="38" width="824" height="924"/>
      <path d="${d.join('')}"/><text x="250" y="518">楚 河</text><text x="650" y="518">汉 界</text></svg>`;
  })();
  const GO_GRID = (() => {
    const d = [];
    for (let i = 0; i < 15; i++) d.push(`M50 ${50 + i * 100}H1450M${50 + i * 100} 50V1450`);
    const stars = [[3, 3], [11, 3], [7, 7], [3, 11], [11, 11]].map(([x, y]) => `<circle cx="${50 + x * 100}" cy="${50 + y * 100}" r="13"/>`);
    return `<svg class="gc-board-lines" viewBox="0 0 1500 1500" aria-hidden="true"><path d="${d.join('')}"/>${stars.join('')}</svg>`;
  })();
  function xqParse(fen) {
    const out = {};
    (fen || XQ_START).split(' ')[0].split('/').forEach((row, i) => {
      let col = 0;
      for (const ch of row) {
        if (/\d/.test(ch)) col += Number(ch);
        else out['abcdefghi'[col++] + (9 - i)] = ch;
      }
    });
    return out;
  }
  const xqAt = (sq, flip) => {
    const c = sq.charCodeAt(0) - 97, r = Number(sq.slice(1)), x = flip ? 8 - c : c, y = flip ? r : 9 - r;
    return `left:${(50 + x * 100) / 9}%;top:${(50 + y * 100) / 10}%`;
  };
  const goAt = (x, y) => `left:${(50 + x * 100) / 15}%;top:${(50 + y * 100) / 15}%`;
  const myTurn = t => !!(t.hand && t.me.player && t.hand.current_player === t.me.player);
  function xqBoard(t, c, h, color) {
    const fen = h?.board?.fen || XQ_START, pieces = xqParse(fen), flip = color === 'black';
    const legal = myTurn(t) && h === t.hand ? h.legal_moves || [] : [];
    const sel = c.sel && legal.some(m => m.startsWith(c.sel)) ? c.sel : null;
    const targets = sel ? legal.filter(m => m.startsWith(sel)).map(m => m.slice(2)) : [];
    const king = fen.split(' ')[1] === 'b' ? 'k' : 'K';
    const last = typeof h?.last_move?.move === 'string' ? [h.last_move.move.slice(0, 2), h.last_move.move.slice(2)] : [];
    const own = ch => (ch === ch.toUpperCase()) === (color === 'red');
    return `
      <div class="gc-xq-board" data-gc-board>${XQ_GRID}
        ${last.map(sq => `<i class="gc-xq-last" style="${xqAt(sq, flip)}"></i>`).join('')}
        ${Object.entries(pieces).map(([sq, ch]) => `
          <button class="gc-xq-piece ${ch === ch.toUpperCase() ? 'red' : 'black'} ${sq === sel ? 'sel' : ''} ${targets.includes(sq) ? 'target' : ''}
            ${h?.in_check && ch === king ? 'check' : ''}" style="${xqAt(sq, flip)}" data-gc-sq="${sq}"
            ${legal.length && (own(ch) || targets.includes(sq)) ? '' : 'tabindex="-1"'}
            aria-label="${ch === ch.toUpperCase() ? '红' : '黑'}${XQ_CHARS[ch]}"><span>${XQ_CHARS[ch]}</span></button>`).join('')}
        ${targets.filter(sq => !pieces[sq]).map(sq => `
          <button class="gc-xq-dot" style="${xqAt(sq, flip)}" data-gc-sq="${sq}" aria-label="走到这里"></button>`).join('')}
      </div>`;
  }
  // The snapshot has no winning line; it ends with the stone that made five, so search from there.
  function goLine(moves) {
    if (!moves.length) return [];
    const side = (moves.length - 1) % 2;
    const own = new Set(moves.filter((_, i) => i % 2 === side).map(([x, y]) => x + ',' + y));
    const [lx, ly] = moves[moves.length - 1];
    for (const [dx, dy] of [[1, 0], [0, 1], [1, 1], [1, -1]]) {
      const line = [[lx, ly]];
      for (const s of [1, -1]) {
        for (let k = 1; own.has(`${lx + dx * k * s},${ly + dy * k * s}`); k++) line.push([lx + dx * k * s, ly + dy * k * s]);
      }
      if (line.length >= 5) return line;
    }
    return [];
  }
  function goBoard(t, c, h) {
    const moves = h?.board?.moves || [];
    const taken = new Set(moves.map(([x, y]) => x + ',' + y));
    const preview = myTurn(t) && h === t.hand && c.preview && !taken.has(c.preview.join(',')) ? c.preview : null;
    const win = h?.result?.reason === 'five' ? goLine(moves) : [];
    return `
      <div class="gc-go-board ${myTurn(t) ? 'gc-turn-mine' : ''}" data-gc-board>${GO_GRID}
        ${moves.map(([x, y], i) => `<i class="gc-stone ${i % 2 ? 'white' : 'black'} ${i === moves.length - 1 ? 'last' : ''}" style="${goAt(x, y)}"></i>`).join('')}
        ${win.map(([x, y]) => `<i class="gc-stone-win" style="${goAt(x, y)}"></i>`).join('')}
        ${preview ? `<i class="gc-stone ghost ${moves.length % 2 ? 'white' : 'black'}" style="${goAt(...preview)}"></i>` : ''}
      </div>`;
  }
  const colorOf = (t, h, seat) => seat && (h?.players?.find(p => p.player === seat.player)?.color || seat.color) || null;
  const sideName = (t, color) => !color ? '' : t.game === 'gomoku' ? color === 'black' ? '执黑' : '执白' : color === 'red' ? '红方' : '黑方';
  function duelBar(t, seat, pos, h) {
    if (!seat) return `
      <div class="gc-duel-bar ${pos} gc-duel-empty">
        <div class="gc-avatar-ring"><span>＋</span></div>
        <div class="grow"><b>等待对手</b><div class="tiny muted">${t.currency === 'beans' ? '机器人马上就位' : '分享桌链接，邀请朋友来下'}</div></div>
      </div>`;
    const color = colorOf(t, h, seat), moving = t.hand?.current_player === seat.player;
    const status = t.hand ? moving ? (seat.bot ? '思考中…' : seat.player === t.me.player ? '轮到你走' : '走棋中')
      + (t.hand.in_check ? ' · <b class="gc-check">被将军</b>' : '') : '等待对方'
      : seat.bot ? '机器人已准备' : seat.ready ? '已准备' : seat.player === t.host ? '房主' : '未准备';
    return `
      <div class="gc-duel-bar ${pos} ${moving ? 'on' : ''}" data-gc-player="${esc(seat.player)}">
        <div class="gc-avatar-ring ${moving ? 'gc-active' : ''}" ${moving ? 'data-gc-ring' : ''}>${avatar(seat, t)}</div>
        <div class="grow">
          <div class="gc-duel-name">${esc(seat.name)}${seat.player === t.host ? '<span class="gc-host">房主</span>' : ''}
            ${color ? `<span class="gc-side ${color}">${sideName(t, color)}</span>` : ''}${label(seat, t)}</div>
          <div class="tiny muted gc-duel-status">${status}</div>
        </div>${h?.clocks ? `
        <div class="gc-clock ${moving ? 'on' : ''}"><b class="num" data-gc-clock="${esc(seat.player)}">--:--</b>${moving ? '<small data-gc-step></small>' : ''}</div>` : ''}
      </div>`;
  }
  function boardOutcome(t, h) {
    const r = h.result || {}, net = r.net?.[t.me.player], won = r.winner === t.me.player;
    const winner = t.seats.find(s => s.player === r.winner)?.name || '对手';
    const why = {
      checkmate: '将死', stalemate: '困毙，无子可走', perpetual_check: '长将判负', repetition: '重复局面，判和',
      move_limit: '60 回合无吃子，判和', insufficient: '双方都没有进攻子力，判和', five: '连成五子', board_full: '棋盘下满，判和',
      draw_agreed: '双方议和', aborted: '开局没走第一步，本局作废、押注退回',
      timeout: net == null ? '超时' : won ? '对方超时' : '你超时了', resign: net == null ? '认输' : won ? '对方认输' : '你认输了',
    }[r.reason] || '';
    const title = r.reason === 'aborted' ? '本局作废' : !r.winner ? '和棋' : won ? '你赢了' : net != null ? '你输了' : `${winner} 胜`;
    return { title, why, net };
  }
  function boardRibbon(t, h) {
    const o = boardOutcome(t, h);
    return `
      <button class="gc-board-result ${o.net > 0 ? 'win' : o.net < 0 ? 'loss' : ''}" data-gc-history>${o.net > 0 ? img('crown') : ''}
        <b>${esc(o.title)}</b><span>${esc(o.why)}${o.net ? ` · ${signed(o.net)} ${unit(t)}` : ''}</span><em>详情 ›</em>
      </button>`;
  }
  function boardControls(t, me, c) {
    const outcome = !t.hand && t.last_hand?.result ? boardRibbon(t, t.last_hand) : '';
    if (t.phase === 'closed') return outcome + '<button class="btn block" data-gc-back>返回游戏中心</button>';
    if (!me) return outcome + `
      <div class="gc-note">你正在观战，入座后就能对弈。${t.currency === 'beans' ? '' : `每局押 ${num(t.rules.stake)} 积分，开局才冻结。`}</div>
      <button class="btn block" data-gc-seat ${disabled(t.seats.length >= 2)}>${t.seats.length >= 2 ? '本桌已满' : '入座 · 不扣分'}</button>`;
    const h = t.hand;
    if (!h) return outcome + controls(c, t, me);
    const legal = h.legal_actions || {}, offer = h.draw_offer, mine = myTurn(t);
    return `
      ${offer && offer.player !== t.me.player ? `
        <div class="gc-draw-offer">对方提出和棋
          <button class="btn sm" data-gc-board-act="accept_draw" ${disabled(!legal.accept_draw)}>接受</button>
          <button class="btn sm ghost" data-gc-board-act="reject_draw" ${disabled(!legal.reject_draw)}>拒绝</button>
        </div>` : offer ? '<div class="gc-wait-reason">已提出和棋，等待对方回应</div>' : ''}
      <div class="gc-actions gc-board-actions">${legal.undo ? `
        <button class="gc-action" data-gc-board-act="undo" ${disabled(false)}>悔棋<small>撤回一个回合</small></button>` : ''}
        <button class="gc-action" data-gc-board-act="offer_draw" ${disabled(!legal.offer_draw)}>求和<small>${offer ? '已提出' : '对方可接受或拒绝'}</small></button>
        <button class="gc-action warm" data-gc-board-act="resign" ${disabled(!legal.resign)}>认输<small>本局判负</small></button>${t.game === 'gomoku' && mine ? `
        <button class="gc-action gold" data-gc-board-act="place" ${disabled(!c.preview)}>落子<small>${c.preview ? '确认这一手' : '先点棋盘选位置'}</small></button>` : ''}
      </div>`;
  }
  function paintBoard(c) {
    const t = c.table, me = t.seats.find(s => s.player === t.me.player), h = t.hand || t.last_hand;
    const firstColor = t.game === 'gomoku' ? 'black' : 'red';
    const bottom = me || t.seats.find(s => colorOf(t, h, s) === firstColor) || t.seats[0] || null;
    const top = t.seats.find(s => s !== bottom) || null;
    const oldScroll = $('#screen').scrollTop;
    setScreen(`
      <section class="gc gc-table gc-board-table gc-${t.game} gc-felt-${Object.hasOwn(tierClasses, t.tier) ? t.tier : 'practice'}"
        data-table-id="${esc(t.id)}" data-revision="${t.revision}" data-phase="${esc(t.phase)}">
        <header class="gc-table-bar gc-board-bar">
          <button class="gc-tool" data-gc-back aria-label="返回">‹</button>
          <div class="grow gc-board-title"><b>${esc(names[t.game])} <span class="gc-tier-chip ${tierClasses[t.tier] || ''}">${esc(t.tier_name)}</span></b>
            <div class="tiny muted"><span class="gc-board-meta">${t.currency === 'beans'
              ? `练习 · ${esc(levels.find(([v]) => v === t.rules.robot_level)?.[1] || '普通')}机器人`
              : `每局押 ${num(t.rules.stake)} 积分 · 第 ${num(t.hand_no || 0)} 局`}</span>
              <button class="gc-connection" data-gc-connection data-state="${c.status}"></button></div>
          </div>
          <button class="gc-tool gc-sound-btn" data-gc-sound aria-label="声音">${icon(sound.pref('sfx') || sound.pref('music') ? 'volume-2' : 'volume-x')}</button>
          <button class="gc-tool" data-gc-rules>玩法</button>
          <button class="gc-tool" data-gc-leave ${disabled(!me)}>离桌</button>
        </header>
        <div data-gc-retry-slot>${pendingHtml()}</div>
        <div class="gc-duel">
          ${duelBar(t, top, 'top', h)}
          <div class="gc-board-frame">${t.game === 'xiangqi' ? xqBoard(t, c, h, colorOf(t, h, bottom) === 'black' ? 'black' : 'red') : goBoard(t, c, h)}
            ${!h ? `
              <div class="gc-board-note"><b>${t.seats.length}<small>/2</small></b><span>等待开局</span><span class="gc-idle" data-gc-idle></span></div>` : ''}
          </div>
          ${duelBar(t, bottom, 'bottom', h)}
        </div>
        <div class="gc-dock gc-board-dock">${boardControls(t, me, c)}</div>
      </section>`);
    $('#screen').scrollTop = oldScroll;
    bindTable(c);
    bindBoard(c);
    tick(c);
    streamStatus(c, c.status);
    drawFx(c);
  }
  function bindBoard(c) {
    const root = $('.gc-table'), board = $('[data-gc-board]', root);
    if (c.table.game === 'xiangqi') $$('[data-gc-sq]', root).forEach(b => b.onclick = () => xqTap(c, b.dataset.gcSq));
    else if (board) board.onclick = e => goTap(c, e, board);
    $$('[data-gc-board-act]', root).forEach(b => b.onclick = () => boardAct(c, b.dataset.gcBoardAct));
  }
  function xqTap(c, sq) {
    const h = c.table.hand;
    if (!myTurn(c.table)) return;
    const legal = h.legal_moves || [];
    if (c.sel && legal.includes(c.sel + sq)) {
      const move = c.sel + sq;
      c.sel = null;
      return action(c, 'move', { move });
    }
    c.sel = c.sel !== sq && legal.some(m => m.startsWith(sq)) ? sq : null;
    if (c.sel) sound.play('tap', .6);
    paintTable(c);
  }
  function goTap(c, e, board) {
    const h = c.table.hand;
    if (!myTurn(c.table)) return;
    const r = board.getBoundingClientRect();
    const x = Math.round(((e.clientX - r.left) / r.width * 1500 - 50) / 100);
    const y = Math.round(((e.clientY - r.top) / r.height * 1500 - 50) / 100);
    if (x < 0 || y < 0 || x > 14 || y > 14 || (h.board?.moves || []).some(([a, b]) => a === x && b === y)) return;
    if (c.preview?.[0] === x && c.preview?.[1] === y) return boardAct(c, 'place');
    c.preview = [x, y];
    sound.play('tap', .6);
    paintTable(c);
  }
  function boardAct(c, op) {
    const t = c.table;
    if (op === 'place') {
      const move = c.preview;
      c.preview = null;
      return move && action(c, 'move', { move });
    }
    if (op !== 'resign') return action(c, op);
    showSheet(`
      <h3>确认认输？</h3>
      <p>本局判负${t.currency === 'beans' ? '。' : `，押注 ${num(t.rules.stake)} 积分归对方。`}</p>
      <button class="btn block danger" data-gc-resign ${disabled(false)}>认输</button>
      <button class="link gc-close" data-cancel>继续下</button>`, (root, close) => {
      $('[data-gc-resign]', root).onclick = async () => { if (await action(c, 'resign')) close(); };
    });
  }
  function boardResult(c, h) {
    if (!h) return;
    const t = c.table, o = boardOutcome(t, h), r = h.result || {};
    showSheet(`
      <div class="gc-result-head">${img('crown')}
        <h3>${esc(o.title)}</h3>
        <span class="tiny muted">${esc(o.why)} · 共 ${num(h.moves?.length || 0)} 手 · 不抽水</span>
      </div>
      ${(h.players || []).map(p => `
        <div class="gc-result-row ${r.winner === p.player ? 'winner' : ''}">
          <div class="grow"><b>${esc(t.seats.find(s => s.player === p.player)?.name || '已离桌玩家')}</b>
            <div class="tiny muted">${sideName(t, p.color)}</div></div>
          <b class="${(r.net?.[p.player] || 0) >= 0 ? 'gc-up' : 'gc-down'}">${r.net?.[p.player] == null ? '—' : signed(r.net[p.player])}</b>
        </div>`).join('')}
      <div class="gc-note">${t.currency === 'beans' ? '练习场只用欢乐豆。' : '押注已结算；和棋或作废原额退回。'}
        <br>可用 ${num(funds(t.me.wallet, t))} ${unit(t)} · 已冻结 ${num(t.me.wallet.frozen)} 积分</div>
      <button class="btn block" data-cancel>回到棋盘</button>`);
  }
  function paintTable(c) {
    if (!live(c)) return;
    if (isBoard(c.table.game)) return paintBoard(c);
    const t = c.table, me = t.seats.find(s => s.player === t.me.player);
    const anchor = me?.seat ?? 0;
    const bottom = me || t.seats.find(s => s.seat === anchor);
    const runoutHand = !t.hand && c.runout ? c.runout.hand : null;
    const shown = runoutHand ? null : celebrating(c), view = t.hand || runoutHand || shown;
    const p = handPlayers(t, view).find(p => p.player === bottom?.player);
    // On Hold'em showdown celebration, highlight the winner's best 5 cards on the board; otherwise highlight my own best 5.
    let highlightBest = p?.best5 || [];
    if (shown && t.game === 'holdem') {
      const winners = shown.result?.winners || [];
      const winBest = winners.map(id => shown.result?.players?.[id]?.best5).find(b => b?.length);
      if (winBest?.length) highlightBest = winBest;
    }
    const myBest = p?.best5 || [];
    const dealAge = c.dealAt ? performance.now() - c.dealAt : Infinity;
    const look = !!t.hand?.legal_actions?.look;
    const timeout = me?.auto && t.me.notice?.includes('超时');
    const timeoutHtml = timeout ? `
      <div class="gc-timeout" role="alert">已超时，进入托管
        <button class="btn sm" data-gc-takeover>取消托管</button>
      </div>` : '';
    const seats = Array.from({ length: t.max_players - 1 }, (_, i) =>
      seatHtml(t.seats.find(s => s.seat === (anchor + i + 1) % t.max_players), i + 1, t, view, highlightBest)).join('');
    const myPos = t.game === 'holdem' && bottom && view ? holdemPositionName(bottom.seat, t, view) : '';
    const logCount = (t.hand?.action_log || t.last_hand?.action_log || []).length;
    const myTurnNow = !!(t.me.player && t.hand?.current_player === t.me.player);
    const turnPulse = myTurnNow && c.turnAlertAt && (performance.now() - c.turnAlertAt < 1800);
    const oldScroll = $('#screen').scrollTop;
    setScreen(`
      <section class="gc gc-table ${t.game === 'holdem' ? 'gc-holdem' : 'gc-zjh'} ${c.raiseBar && t.game === 'holdem' ? 'gc-raise-open' : ''} gc-felt-${Object.hasOwn(tierClasses, t.tier) ? t.tier : 'practice'} ${dealAge < 1200 ? 'gc-dealing' : ''}"
        style="--deal-age:-${Math.round(Math.min(dealAge, 1200))}ms" data-table-id="${esc(t.id)}" data-revision="${t.revision}" data-phase="${esc(t.phase)}">
        <header class="gc-table-bar">
          <button class="gc-tool" data-gc-back>‹ 返回</button>
          <div class="grow"><b>${esc(names[t.game])} <span class="gc-tier-chip ${tierClasses[t.tier] || ''}">${esc(t.tier_name)}</span></b>
            <div class="tiny muted">${t.game === 'zjh' ? `底注 ${num(t.rules.ante)} · ${t.rules.cap == null ? '不封顶' : '封顶 ' + num(t.rules.cap)}` : `无限注 · 盲注 ${num(t.rules.small_blind)}/${num(t.rules.big_blind)}`} · 不抽水</div>
          </div>
          <button class="gc-tool gc-sound-btn" type="button" data-gc-chat aria-label="快捷语" ${disabled(!me || t.phase === 'closed')}>💬</button>
          <button class="gc-tool gc-sound-btn" type="button" data-gc-hand-ranks aria-label="牌型参考">🃏</button>
          <button class="gc-tool gc-sound-btn" data-gc-sound aria-label="声音">${icon(sound.pref('sfx') || sound.pref('music') ? 'volume-2' : 'volume-x')}</button>
          <button class="gc-tool" data-gc-rules>玩法</button>
        </header>
        <div class="gc-tools">
          <button class="gc-auto ${me?.auto ? 'on' : ''}" role="switch" aria-checked="${!!me?.auto}" aria-label="托管" data-gc-auto ${disabled(!me || me.leave_after)}><i></i>${me?.auto ? '取消托管' : '托管'}</button>
          ${t.game === 'holdem' ? `
            <button class="gc-tool ${me?.away ? 'on' : ''}" data-gc-away ${disabled(!me || me.leave_after)}>暂离${me?.away ? '中' : ''}</button>
            <button class="gc-tool" data-gc-topup ${disabled(!me || me.leave_after || t.phase === 'playing')}>补码</button>
            <button class="gc-tool ${bbMode ? 'on' : ''}" type="button" data-gc-bb-toggle title="切换筹码 / 大盲(BB)显示">${bbMode ? 'BB' : '筹码'}</button>
            <button class="gc-tool" type="button" data-gc-action-log ${logCount ? '' : 'disabled'}>记录</button>` : ''}
          <button class="link" data-gc-history ${c.history.length ? '' : 'disabled'}>${t.game === 'holdem' ? '最近手牌' : '上手结算'}</button>
          <button class="link" data-gc-leave ${disabled(!me)}>离桌</button>
        </div>
        <div class="gc-connection-row">
          <button class="gc-connection" data-gc-connection data-state="${c.status}"></button>
          ${timeout && t.game === 'holdem' ? timeoutHtml
            : t.currency === 'beans' ? '<span>练习场 · 点玩家头像可送花/丢炸弹</span>' : '<button class="link" data-gc-broadcast>群播报说明</button>'}
        </div>
        ${t.game === 'zjh' ? timeoutHtml : ''}
        <div data-gc-retry-slot>${pendingHtml()}</div>
        <div class="gc-felt">${croupier(t)}
          <div class="gc-oval"></div>
          ${seats}${tableCenter(c, t, shown, highlightBest)}
        </div>
        <div class="gc-dock ${myTurnNow ? 'gc-my-turn' : ''} ${turnPulse ? 'gc-turn-pulse' : ''}">${bottom ? `
          <div class="gc-me-row">
            <div class="gc-avatar-ring ${t.hand?.current_player === bottom.player ? 'gc-active' : ''}"
              ${t.hand?.current_player === bottom.player ? 'data-gc-ring' : ''}>
              ${avatar(bottom, t)}${markers(bottom, t)}
              <span class="gc-seat-status ${bottom.auto ? 'gc-auto-status' : ''}">${seatStatus(bottom, t, p)}</span>
            </div>
            <div class="grow">
              <div class="gc-me-name">
                ${esc(bottom.name)}${bottom.player === t.host ? '<span class="gc-host">房主</span>' : ''}${myPos ? `<span class="gc-pos-tag">${esc(myPos)}</span>` : ''}${label(bottom, t)}
                ${p && (t.game !== 'holdem' || p.bet > 0) ? `<span class="gc-bet num">${img('chip')} ${fmtChips(t.game === 'holdem' ? p.bet : p.invested, t)}</span>` : ''}
              </div>
              ${titlePlaque(bottom, t)}
              <div class="tiny muted">${me ? `${t.game === 'holdem' ? '桌上筹码 ' + fmtChips(p?.stack ?? me.stack, t) + ' · ' : ''}可用 ${num(funds(t.me.wallet, t))} ${unit(t)} · 已冻结 ${num(t.me.wallet.frozen)} 积分` : '旁观视角 · 不展示未公开手牌'}</div>
            </div>
          </div>` : ''}
      ${view ? `
          <button class="gc-hand" aria-label="${look ? '看牌' : '自己的手牌'}" ${look ? `data-gc-action="look" ${disabled(false)}` : 'disabled'}>
            ${p ? cards(p.cards, t.game === 'zjh' ? 3 : 2, 'lg', shown ? highlightBest : myBest) : '<span class="muted">本手旁观，从下一手加入</span>'}
          </button>
          <div class="gc-hand-label">${handRankHtml(view, shown, look, p, t)}</div>` : `
          <div class="gc-wait-message">${t.last_hand ? `
            <div class="gc-result-peek-row">
              <button class="gc-result-peek" data-gc-history>
                ${img('crown')} 第 ${t.last_hand.number} 手已结算${t.last_hand.result?.net?.[t.me.player] != null
                  ? ' · 本手 ' + signed(t.last_hand.result.net[t.me.player]) : ''} <span>查看 ›</span>
              </button>
              ${t.last_hand.can_reveal ? `<button class="btn sm gc-reveal-btn" type="button" data-gc-reveal="${t.last_hand.number}">🃏 亮牌</button>` : ''}
            </div>` : `${img('emblem-' + t.game)}
            <span>${me ? t.game === 'holdem' ? '首手房主开始，满 6 人自动开局' : '房主开始或全员准备后开局' : '先看桌，再决定是否入座'}
              <small>${t.game === 'zjh' ? '入座不扣分 · 每步 30 秒' : '首手房主开始 · 之后连续发牌'}</small>
            </span>`}</div>`}
      ${controls(c, t, me)}${t.hand?.comparisons?.length ? '<button class="link gc-close" data-gc-comparisons>查看仅比牌双方可见的结果</button>' : ''}</div>
      </section>`);
    $('#screen').scrollTop = oldScroll;
    bindTable(c);
    tick(c);
    streamStatus(c, c.status);
    drawFx(c);
  }
  function handRankHtml(view, shown, look, p, t) {
    if (!p) return '<span class="muted">本手旁观中</span>';
    if (shown) {
      const net = shown.result?.net?.[t.me.player];
      const delta = net > 0 ? ` · 赢 +${num(net)}` : net < 0 ? ` · 输 −${num(-net)}` : '';
      const privacy = shown.revealed?.includes(t.me.player) ? ' · 已亮牌' : ' · 默认未亮牌';
      return p.hand_name
        ? `<span class="gc-hand-badge">🃏 最大牌型：<b>${esc(p.hand_name)}</b>${esc(delta + privacy)}</span>`
        : esc('本手已结束' + delta);
    }
    if (look) return '<span class="gc-hand-hint">🔒 闷牌中 · 点手牌看牌显示当前最大牌型</span>';
    // Show subtle pot-odds pill on Hold'em when it's your turn facing a bet.
    let oddsHtml = '';
    if (t.game === 'holdem' && t.hand && t.hand.current_player === t.me.player && t.hand.legal_actions?.call?.amount > 0) {
      const callAmt = t.hand.legal_actions.call.amount;
      const pot = t.hand.engine?.pot || 0;
      const pct = Math.round(callAmt / Math.max(1, pot + callAmt) * 100);
      oddsHtml = ` <span class="gc-odds-pill" title="需跟注额占跟注后总底池比例">赔率 ${pct}%</span>`;
    }
    if (p.hand_name) {
      return `<span class="gc-hand-badge ${p.folded ? 'dim' : ''}">🃏 当前最大：<b>${esc(p.hand_name)}</b>${p.folded ? '（已弃牌）' : ''}</span>${oddsHtml}`;
    }
    return esc(p.folded ? '已弃牌' : p.seen ? '已看牌' : '等待发牌') + oddsHtml;
  }
  function tick(c) {
    if (!live(c)) return;
    const t = c.table, seconds = Math.max(0, Math.ceil((t.turn_deadline || now(c)) - now(c)));
    $$('[data-gc-seconds]').forEach(el => el.textContent = seconds);
    $$('[data-gc-countdown]').forEach(el => el.textContent = Math.max(0, Math.ceil(t.countdown_at - now(c))));
    const idle = Math.max(0, Math.ceil((t.idle_deadline || 0) - now(c)));
    $$('[data-gc-idle]').forEach(el => el.textContent = t.idle_deadline && t.phase === 'waiting'
      ? `${Math.floor(idle / 60)} 分 ${String(idle % 60).padStart(2, '0')} 秒内无人操作将自动散桌` : '');
    if (isBoard(t.game)) {
      const h = t.hand || t.last_hand, clocks = h?.clocks;
      $$('[data-gc-clock]').forEach(el => {
        const p = el.dataset.gcClock;
        let left = clocks?.remaining?.[p];
        if (left == null) return;
        if (t.hand && clocks.running === p) left -= now(c) - t.server_time;
        left = Math.max(0, Math.ceil(left));
        el.textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
        el.parentElement.classList.toggle('low', left < 60);
      });
      $$('[data-gc-step]').forEach(el => el.textContent = `本步 ${seconds}s`);
    }
    if (t.me.player && t.hand?.current_player === t.me.player && seconds > 0 && seconds <= 5 && c.ticked !== seconds) {
      c.ticked = seconds;
      sound.play('tick', .78);
      haptic.impact(seconds <= 2 ? 'heavy' : 'light');
    }
    $$('[data-gc-ring]').forEach(el => {
      el.style.setProperty('--gc-progress', `${Math.min(100, seconds / (t.rules.step_seconds || 30) * 100)}%`);
      el.style.setProperty('--gc-ring-color', seconds < 10 ? '#ff6b81' : '#43d39e');
    });
  }
  function tableWrite(c, op, body = {}) {
    return write(`/tables/${encodeURIComponent(c.id)}/${op}`, body, data => accept(c, data.table, true), () => live(c));
  }
  function action(c, op, extra = {}) {
    const t = c.table;
    if (!t.hand?.legal_actions?.[op]) return toast('牌局已变化，请按最新按钮操作', true);
    return tableWrite(c, 'actions', { hand_no: t.hand_no, turn: t.hand.turn, action: op, ...extra });
  }
  function bindTable(c) {
    const root = $('.gc-table'), t = c.table, me = t.seats.find(s => s.player === t.me.player);
    $$('[data-gc-back]', root).forEach(b => b.onclick = back);
    $('[data-gc-connection]', root).onclick = () => connect(c);
    $$('[data-gc-rules]', root).forEach(b => b.onclick = () => rules(t.game, t.rules));
    $('[data-gc-sound]', root).onclick = soundSheet;
    $$('[data-gc-history]', root).forEach(b => b.onclick = () => isBoard(t.game) ? boardResult(c, t.last_hand) : resultSheet(c, c.history[0]));
    const auto = $('[data-gc-auto]', root);
    if (auto) auto.onclick = () => tableWrite(c, 'auto', { enabled: !me.auto });
    const takeover = $('[data-gc-takeover]', root);
    if (takeover) takeover.onclick = () => tableWrite(c, 'auto', { enabled: false });
    const ready = $('[data-gc-ready]', root);
    if (ready) ready.onclick = () => tableWrite(c, 'ready', { ready: !me.ready });
    const start = $('[data-gc-start]', root);
    if (start) start.onclick = () => tableWrite(c, 'start');
    const away = $('[data-gc-away]', root);
    if (away) away.onclick = () => tableWrite(c, 'away', { enabled: !me.away });
    const topup = $('[data-gc-topup]', root);
    if (topup) topup.onclick = () => buySheet({ ...t.rules, currency: t.currency, name: t.tier_name }, t.me.wallet, true, me.stack, amount => tableWrite(c, 'top-up', { amount }));
    $('[data-gc-leave]', root).onclick = () => leave(c);
    const broadcastBtn = $('[data-gc-broadcast]', root);
    if (broadcastBtn) broadcastBtn.onclick = () => broadcast(t);
    const seat = $('[data-gc-seat]', root);
    if (seat) seat.onclick = () => t.game === 'holdem' ? buySheet({ ...t.rules, currency: t.currency, name: t.tier_name }, t.me.wallet, false, 0, amount => tableWrite(c, 'seat', { buy_in: amount })) : tableWrite(c, 'seat');
    $$('[data-gc-action]', root).forEach(b => b.onclick = () => {
      const op = b.dataset.gcAction;
      if (op === 'compare') return compareSheet(c);
      if (t.game === 'holdem' && ['raise', 'bet'].includes(op)) {
        haptic.select();
        const legal = t.hand?.legal_actions?.[op];
        if (!legal) return;
        if (c.raiseBar && c.raiseBar.op === op && c.raiseBar.turn === t.hand.turn) {
          c.raiseBar = null;
        } else {
          c.raiseBar = { op, value: legal.min, handNo: t.hand.number, turn: t.hand.turn };
        }
        return paintTable(c);
      }
      if (op === 'all_in') { haptic.impact('medium'); return confirmAction(c, op); }
      if (t.game === 'holdem' && op === 'fold' && t.hand?.legal_actions?.check) {
        haptic.notify('warning');
        return confirmFreeFold(c);
      }
      c.raiseBar = null;
      haptic.impact(op === 'fold' ? 'light' : 'medium');
      action(c, op);
    });
    const raiseInline = $('[data-gc-raise-inline]', root);
    if (raiseInline && c.raiseBar && t.hand) {
      const op = c.raiseBar.op, h = t.hand, legal = h.legal_actions?.[op];
      if (legal) {
        const range = $('.gc-range', raiseInline);
        const bb = Math.max(1, t.rules?.big_blind || 1);
        const valEl = $('[data-gc-raise-val]', raiseInline);
        const confirmBtn = $('[data-gc-raise-confirm]', raiseInline);
        const updateInline = v => {
          const next = Math.max(legal.min, Math.min(legal.max, Math.round(v)));
          c.raiseBar.value = next;
          range.value = String(next);
          const bbText = (next / bb).toFixed(1).replace(/\.0$/, '');
          valEl.innerHTML = `${num(next)} <small>(${bbText} BB)</small>`;
          const isAll = next >= legal.max;
          confirmBtn.textContent = isAll ? '全下' : op === 'raise' ? '确认加注' : '确认下注';
          confirmBtn.classList.toggle('allin', isAll);
          $$('[data-gc-preset-val]', raiseInline).forEach(btn => btn.classList.toggle('on', Number(btn.dataset.gcPresetVal) === next));
        };
        const endDrag = () => {
          if (!c.draggingRaise) return;
          c.draggingRaise = false;
          if (c.deferredPaint) { c.deferredPaint = false; paintTable(c); }
        };
        range.onpointerdown = () => { c.draggingRaise = true; };
        range.onpointerup = endDrag;
        range.onpointercancel = endDrag;
        range.onchange = endDrag;
        range.oninput = () => { haptic.select(60); updateInline(Number(range.value)); };
        $$('[data-gc-preset-val]', raiseInline).forEach(btn => btn.onclick = () => {
          haptic.select();
          updateInline(Number(btn.dataset.gcPresetVal));
        });
        $$('[data-gc-raise-step]', raiseInline).forEach(btn => btn.onclick = () => {
          haptic.select();
          updateInline((c.raiseBar.value ?? legal.min) + Number(btn.dataset.gcRaiseStep));
        });
        $('[data-gc-raise-cancel]', raiseInline).onclick = () => { c.raiseBar = null; paintTable(c); };
        confirmBtn.onclick = async () => {
          if (c.table.hand?.turn !== h.turn || c.table.hand_no !== h.number) {
            c.raiseBar = null; paintTable(c); return toast('牌局已变化，请重新选择', true);
          }
          const amount = c.raiseBar.value;
          haptic.impact(amount >= legal.max ? 'heavy' : 'medium');
          c.raiseBar = null;
          await action(c, op, { amount });
        };
      }
    }
    $$('[data-gc-pre]', root).forEach(b => b.onclick = () => {
      if (!t.hand || b.disabled) return;
      haptic.select();
      const kind = b.dataset.gcPre;
      const amt = Number(b.dataset.gcPreAmt || 0);
      const street = t.hand.engine?.street || 'preflop';
      if (c.preAction && c.preAction.kind === kind && c.preAction.handNo === t.hand.number && c.preAction.street === street) {
        c.preAction = null;
      } else {
        c.preAction = { kind, amount: amt, handNo: t.hand.number, street };
      }
      paintTable(c);
    });
    $$('[data-gc-bb-toggle]', root).forEach(el => el.onclick = e => {
      e.stopPropagation();
      haptic.select();
      setBbMode(!bbMode);
      paintTable(c);
    });
    const logBtn = $('[data-gc-action-log]', root);
    if (logBtn) logBtn.onclick = () => actionLogSheet(c);
    const comparison = $('[data-gc-comparisons]', root);
    if (comparison) comparison.onclick = () => comparisonSheet(c);
    $$('[data-gc-reveal]', root).forEach(b => b.onclick = e => {
      e.stopPropagation();
      tableWrite(c, 'reveal', { hand_no: Number(b.dataset.gcReveal) });
    });
    $$('[data-gc-interact-seat]', root).forEach(el => el.onclick = e => {
      e.stopPropagation();
      const target = t.seats.find(s => s.player === el.dataset.gcInteractSeat);
      if (target) interactSheet(c, target);
    });
    const chatBtn = $('[data-gc-chat]', root);
    if (chatBtn) chatBtn.onclick = () => phraseSheet(c);
    $$('[data-gc-hand-ranks]', root).forEach(b => b.onclick = () => handRanksSheet(t.game, t.rules, t));
    bindPending(root);
  }
  function interactSheet(c, target) {
    const t = c.table, isBeans = t.currency === 'beans', avail = funds(t.me.wallet, t);
    showSheet(`
      <div class="gc-interact-head">
        ${avatar(target, t)}
        <div class="grow">
          <h3>与 ${esc(target.name)} 互动</h3>
          <div class="tiny muted">可用 ${num(avail)} ${unit(t)} · 点击道具立即投掷</div>
        </div>
      </div>
      <div class="gc-prop-grid">${PROPS.map(([id, emoji, name, pts, bns, desc]) => {
        const cost = isBeans ? bns : pts;
        return `
          <button class="gc-prop-card" type="button" data-gc-prop="${id}" ${disabled(avail < cost)}>
            <span class="gc-prop-emoji">${emoji}</span>
            <b>${esc(name)}</b>
            <small class="muted">${esc(desc)}</small>
            <em class="num">${num(cost)} ${unit(t)}</em>
          </button>`;
      }).join('')}</div>
      <button class="link gc-close" data-cancel>取消</button>`, (root, close) => {
      $$('[data-gc-prop]', root).forEach(b => b.onclick = async () => {
        const item = b.dataset.gcProp;
        close();
        await tableWrite(c, 'interact', { kind: 'prop', item, target: target.player });
      });
    });
  }
  const HOLDEM_HAND_RANKS = [
    { rank: 1, name: '皇家同花顺', desc: '同花色 10-J-Q-K-A（最大）', cards: [12, 11, 10, 9, 8] },
    { rank: 2, name: '同花顺', desc: '同花色五张连号（A2345 最小）', cards: [20, 19, 18, 17, 16] },
    { rank: 3, name: '四条', desc: '四张同点数 + 任意单牌', cards: [12, 25, 38, 51, 11] },
    { rank: 4, name: '葫芦', desc: '三张同点数 + 一对', cards: [11, 24, 37, 10, 23] },
    { rank: 5, name: '同花', desc: '五张同花色，不连号', cards: [25, 22, 20, 17, 14] },
    { rank: 6, name: '顺子', desc: '五张不同花色连号', cards: [10, 22, 34, 46, 6] },
    { rank: 7, name: '三条', desc: '三张同点数 + 两张单牌', cards: [9, 22, 35, 12, 46] },
    { rank: 8, name: '两对', desc: '两组对子 + 一张单牌', cards: [12, 25, 36, 49, 7] },
    { rank: 9, name: '一对', desc: '一组对子 + 三张单牌', cards: [11, 24, 38, 47, 5] },
    { rank: 10, name: '高牌', desc: '未成以上牌型，依次比最大单张', cards: [12, 22, 33, 44, 1] },
  ];
  const ZJH_HAND_RANKS = [
    { rank: 1, name: '豹子', desc: '三张相同点数（AAA 最大）', cards: [12, 25, 38] },
    { rank: 2, name: '顺金', desc: '同花色三张连号（QKA 最大，A23 最小）', cards: [25, 24, 23] },
    { rank: 3, name: '金花', desc: '三张同花色，不连号', cards: [12, 9, 6] },
    { rank: 4, name: '顺子', desc: '三张不同花色连号（QKA 最大，A23 最小）', cards: [12, 24, 36] },
    { rank: 5, name: '对子', desc: '两张相同点数 + 一张单牌', cards: [11, 24, 38] },
    { rank: 6, name: '单张', desc: '散牌，按最大单张依次比较', cards: [12, 22, 33] },
  ];
  function handRanksSheet(game = 'holdem', config = {}, t = null) {
    const zjh = game === 'zjh';
    const items = zjh ? ZJH_HAND_RANKS : HOLDEM_HAND_RANKS;
    const myPlayer = t && (t.hand || t.last_hand) ? handPlayers(t, t.hand || t.last_hand).find(p => p.player === t.me?.player) : null;
    const currentName = myPlayer?.hand_name ? String(myPlayer.hand_name).split(/[·\s]/)[0] : '';
    const extra235 = zjh ? `
      <div class="gc-rank-row gc-rank-special ${config.allow_235 ? 'on' : ''}">
        <div class="gc-rank-meta">
          <div class="gc-rank-title"><span class="gc-rank-no">特</span><b>235 特殊牌</b>${config.allow_235 ? '<span class="gc-rank-badge">本桌已开启</span>' : '<span class="gc-rank-badge off">本桌未开启</span>'}</div>
          <div class="tiny muted">${config.allow_235 ? '不同花色 2-3-5 专吃豹子；无豹子时按普通单张算' : '当前场次未启用 235 吃豹子规则'}</div>
        </div>
        <div class="gc-rank-cards">${cards([0, 14, 29], 3, 'xs')}</div>
      </div>` : '';
    showSheet(`
      <h3>🃏 ${names[game]} · 牌型大小参考</h3>
      <div class="tiny muted" style="margin:2px 0 10px">从大到小依次排列 · 同牌型按关键牌点数比较，不比花色</div>
      <div class="gc-rank-list">
        ${items.map(it => {
          const hit = currentName && (currentName === it.name || it.name.startsWith(currentName));
          return `
          <div class="gc-rank-row ${it.rank <= 3 ? 'top' : ''} ${hit ? 'cur' : ''}">
            <div class="gc-rank-meta">
              <div class="gc-rank-title"><span class="gc-rank-no">${it.rank}</span><b>${esc(it.name)}</b>${hit ? '<span class="gc-rank-badge cur">当前手牌</span>' : ''}</div>
              <div class="tiny muted">${esc(it.desc)}</div>
            </div>
            <div class="gc-rank-cards">${cards(it.cards, it.cards.length, 'xs')}</div>
          </div>`;
        }).join('')}
        ${extra235}
      </div>
      <button class="btn block" data-cancel>知道了</button>`);
  }
  function phraseSheet(c) {
    showSheet(`
      <h3>💬 快捷互动语</h3>
      <div class="tiny muted" style="margin:2px 0 10px">点击立即在牌桌头像上方弹出气泡（免费）</div>
      <div class="gc-phrase-list">${PHRASES.map(([id, text]) => `
        <button class="gc-phrase-item" type="button" data-gc-phrase="${id}">
          <span>${esc(text)}</span><i>发送 ›</i>
        </button>`).join('')}</div>
      <button class="link gc-close" data-cancel>关闭</button>`, (root, close) => {
      $$('[data-gc-phrase]', root).forEach(b => b.onclick = async () => {
        const phrase_id = b.dataset.gcPhrase;
        close();
        await tableWrite(c, 'interact', { kind: 'phrase', phrase_id });
      });
    });
  }
  function actionLogSheet(c) {
    const t = c.table, h = t.hand || t.last_hand;
    const logs = h?.action_log || [];
    const nameOf = id => esc(t.seats.find(s => s.player === id)?.name || '已离桌玩家');
    const grouped = [];
    for (const item of logs) {
      const st = item.street || 'preflop';
      let group = grouped.at(-1);
      if (!group || group.street !== st) {
        group = { street: st, items: [] };
        grouped.push(group);
      }
      group.items.push(item);
    }
    const body = grouped.length ? grouped.map(g => `
      <div class="gc-log-group">
        <div class="gc-log-street">${esc(STREET_ZH[g.street] || g.street)}</div>
        ${g.items.map(it => {
          const act = it.all_in ? 'all_in' : it.action;
          const zh = HOLDEM_ACTION_ZH[act] || act;
          const val = it.action === 'raise' || it.action === 'bet'
            ? `到 ${fmtChips(it.to, t)}`
            : it.amount > 0 ? fmtChips(it.amount, t) : '';
          return `
            <div class="gc-log-row">
              <b>${nameOf(it.player)}</b>
              <span class="gc-log-act gc-act-${esc(act)}">${esc(zh)}${val ? ' ' + esc(val) : ''}</span>
              ${it.auto ? '<span class="tiny muted">托管</span>' : ''}
            </div>`;
        }).join('')}
      </div>`).join('') : '<div class="empty">本手暂无行动记录</div>';
    showSheet(`
      <h3>📜 第 ${h?.number || 0} 手行动记录</h3>
      <div class="tiny muted" style="margin:2px 0 10px">盲注 ${num(t.rules.small_blind)}/${num(t.rules.big_blind)} · 按街次顺序记录</div>
      <div class="gc-log-list">${body}</div>
      <button class="btn block" data-cancel>回到牌桌</button>`);
  }
  function soundSheet() {
    const row = (kind, title, note) => `
      <button class="gc-switch-row" role="switch" aria-checked="${sound.pref(kind)}" data-gc-sound-kind="${kind}">
        <span><b>${title}</b><small>${note}</small></span><i></i>
      </button>`;
    showSheet(`
      <h3>声音与触感</h3>
      ${row('music', '背景音乐', '只在牌桌播放，切到后台自动暂停')}
      ${row('sfx', '音效', '发牌、下注、弃牌、轮到你和输赢提示')}
      ${row('haptic', '触感反馈', '轮到行动、加注和摊牌时轻震动（Telegram 客户端支持）')}
      <button class="btn block" data-cancel>好的</button>`, root => {
      $$('[data-gc-sound-kind]', root).forEach(b => b.onclick = () => {
        const on = b.getAttribute('aria-checked') !== 'true';
        sound.set(b.dataset.gcSoundKind, on);
        b.setAttribute('aria-checked', String(on));
        if (on && b.dataset.gcSoundKind === 'sfx') sound.play('tap');
        if (on && b.dataset.gcSoundKind === 'haptic') haptic.impact('light');
        $('[data-gc-sound] use')?.setAttribute('href', `#i-${sound.pref('sfx') || sound.pref('music') ? 'volume-2' : 'volume-x'}`);
      });
    });
  }
  function timeoutSheet(c) {
    showSheet(`
      <h3>已超时，进入托管</h3>
      <p>刚才没有及时操作，系统已按托管策略替你出手。托管会一直开着，点下面的按钮就能接管。</p>
      <div class="gc-note">${c.table.game === 'zjh' ? '先看牌，有对子或更好才跟注，单张就弃牌；不加注、不主动比牌。' : '能免费过就过；有成牌且跟注不超过半池才跟，否则弃牌。'}</div>
      <button class="btn block" data-gc-takeover-sheet ${disabled(false)}>取消托管</button>
      <button class="link gc-close" data-cancel>继续托管</button>`, (root, close) => {
      $('[data-gc-takeover-sheet]', root).onclick = async () => { if (await tableWrite(c, 'auto', { enabled: false })) close(); };
    });
  }
  function leave(c) {
    const t = c.table, p = handPlayers(t).find(x => x.player === t.me.player);
    const holdemText = !p ? '离桌后桌上筹码立即退回可用积分。'
      : p.all_in ? '你已全下，仍参与本手结算；结算后退回剩余筹码。'
      : p.folded ? '你已弃牌，本手结算后退回剩余筹码。' : '申请后轮到你时自动弃牌，不再投入；本手结算后退回剩余筹码。';
    showSheet(`
      <h3>${t.game === 'holdem' && p ? '本手结算后离桌' : '确认离桌？'}</h3>
      <p>${t.game === 'zjh' ? p ? '正在玩的这一手会按弃牌处理；已投入的积分不退，结算后退回剩余额度。' : '你不在正在进行的牌局里，离桌不影响积分。' : holdemText}</p>
      <div class="gc-note">只是想切回大厅？点「返回」即可，不必离桌。房主离桌后会自动移交给最早入座的玩家。</div>
      <button class="btn block" data-gc-leave-confirm ${disabled(false)}>确认离桌</button>
      <button class="link gc-close" data-cancel>继续玩</button>`, (root, close) => {
      $('[data-gc-leave-confirm]', root).onclick = async () => { if (await tableWrite(c, 'leave')) { close(); if (c.table.me.seat == null) back(); else toast('已安排本手结算后离桌'); } };
    });
  }
  function compareSheet(c) {
    const t = c.table, hand = t.hand, legal = hand.legal_actions.compare;
    if (!legal) return;
    showSheet(`
      <h3>选一位玩家比牌</h3>
      <p class="small muted">支付 ${num(legal.amount)} ${unit(t)}；一样大时，发起比牌的人输。只有双方能看到这次比牌牌面。</p>
      <div class="gc-compare-targets">${legal.targets.map(id => `
        <button class="btn ghost" data-gc-target="${esc(id)}" ${disabled(false)}>${esc(t.seats.find(s => s.player === id)?.name || '玩家')}</button>`).join('')}</div>
      <button class="link gc-close" data-cancel>取消</button>`, (root, close) => {
      $$('[data-gc-target]', root).forEach(b => b.onclick = async () => {
        if (c.table.hand_no !== hand.number || c.table.hand?.turn !== hand.turn) { close(); return toast('牌局已变化，请重新选择', true); }
        if (await action(c, 'compare', { target: b.dataset.gcTarget })) { close(); comparisonSheet(c); }
      });
    });
  }
  function comparisonSheet(c) {
    const h = c.table.hand || c.table.last_hand;
    showSheet(`
      <h3>比牌结果 · 仅双方可见</h3>
      ${(h?.comparisons || []).map(x => `
        <div class="gc-compare-result">${[x.actor, x.target].map(id => `
          <div><b>${esc(c.table.seats.find(s => s.player === id)?.name || '玩家')} · ${id === x.loser ? '输' : '赢'}</b>
            <div>${cards(x.cards?.[id], 3, 'sm')}</div>
          </div>`).join('<span>VS</span>')}</div>`).join('')}
      <button class="btn block" data-cancel>回到牌桌</button>`);
  }
  function confirmAction(c, op) {
    const h = c.table.hand;
    showSheet(`
      <h3>${c.table.game === 'zjh' ? '孤注一掷？' : '确认全下？'}</h3>
      <p>${c.table.game === 'zjh' ? '投入本手全部剩余额度，所有还在局的玩家一起摊牌。' : '投入你的全部剩余筹码；即使筹码更少，也只参与自己有份的底池。'}</p>
      <button class="btn block" data-gc-confirm ${disabled(false)}>确认</button>
      <button class="link gc-close" data-cancel>取消</button>`, (root, close) => {
      $('[data-gc-confirm]', root).onclick = async () => { if (c.table.hand?.turn !== h.turn || c.table.hand_no !== h.number) { close(); return toast('牌局已变化，请重新选择', true); } if (await action(c, op)) close(); };
    });
  }
  function confirmFreeFold(c) {
    const h = c.table.hand;
    showSheet(`
      <h3>当前可以免费过牌</h3>
      <p>现在无需补任何筹码就能继续看牌，确定要直接弃牌吗？</p>
      <button class="btn block" data-gc-free-check ${disabled(false)}>改为过牌（免费）</button>
      <button class="btn block danger" style="margin-top:8px" data-gc-free-fold ${disabled(false)}>仍然弃牌</button>
      <button class="link gc-close" data-cancel>取消</button>`, (root, close) => {
      $('[data-gc-free-check]', root).onclick = async () => {
        if (c.table.hand?.turn !== h.turn || c.table.hand_no !== h.number) { close(); return toast('牌局已变化，请重新选择', true); }
        haptic.impact('medium');
        c.raiseBar = null;
        if (await action(c, 'check')) close();
      };
      $('[data-gc-free-fold]', root).onclick = async () => {
        if (c.table.hand?.turn !== h.turn || c.table.hand_no !== h.number) { close(); return toast('牌局已变化，请重新选择', true); }
        haptic.impact('light');
        c.raiseBar = null;
        if (await action(c, 'fold')) close();
      };
    });
  }
  function buySheet(tier, w, topup, stack, submit) {
    const min = topup ? 1 : tier.buy_min;
    const max = Math.max(0, Math.min(funds(w, tier), tier.buy_max == null ? funds(w, tier) : tier.buy_max - (topup ? stack : 0)));
    let value = Math.min(max, topup ? Math.max(min, (tier.big_blind || 1) * 100 - stack) : (tier.big_blind || 1) * 100);
    const impossible = max < min;
    value = Math.max(min, value);
    showSheet(`
      <h3>${topup ? '补码' : '带入'} · ${esc(tier.name)}</h3>
      <div class="gc-buy-value num" data-gc-buy-value>${num(value)}</div>
      <div class="gc-center tiny muted">${topup ? '增加的筹码' : '冻结为桌上筹码'} · ${unit(tier)}</div>
      <input class="gc-range" type="range" aria-label="${topup ? '补码金额' : '带入金额'}" min="${min}" max="${Math.max(min, max)}" value="${value}" step="1" ${impossible || min === max ? 'disabled' : ''}>
      <label class="gc-amount-label">精确金额<input class="field" type="number" inputmode="numeric" min="${min}" max="${max}" value="${value}" data-gc-amount></label>
      <div class="gc-buy-limits"><span>最低 ${num(min)}</span><span>${tier.buy_max == null ? '场次无上限 · 当前可带 ' : '最高 '}${num(max)}</span></div>
      <div class="gc-note">可用 ${num(funds(w, tier))} ${unit(tier)} · 已冻结 ${num(w.frozen)} 积分
        <br>${topup ? '只在两手之间补码；提交时以服务端阶段和筹码为准，不会暂停整桌发牌。' : '确认后冻结带入，离桌把剩余筹码退回可用余额。'}
        ${tier.currency === 'beans' ? topup ? '<br>练习场补码后最多 2,000 欢乐豆，不花积分。' : '<br>练习场固定带入 2,000 欢乐豆，不花积分。' : ''}
      </div>
      <div class="gc-form-error" role="status">${impossible ? '余额不足或桌上筹码已达上限' : ''}</div>
      <div data-gc-retry-slot>${pendingHtml()}</div>
      <button class="btn block" data-gc-buy-confirm ${disabled(impossible)}>确认${topup ? '补码' : '带入'}</button>
      <button class="link gc-close" data-cancel>取消</button>`, (root, close) => {
      const range = $('.gc-range', root), input = $('[data-gc-amount]', root), button = $('[data-gc-buy-confirm]', root);
      const update = source => {
        value = Number(source.value);
        range.value = value; input.value = value;
        $('[data-gc-buy-value]', root).textContent = num(value);
        const invalid = !Number.isSafeInteger(value) || value < min || value > max;
        button.dataset.gcDisabled = String(invalid); button.disabled = invalid || busy;
        $('.gc-form-error', root).textContent = invalid ? `请输入 ${num(min)}–${num(max)} 的整数` : '';
      };
      range.oninput = () => update(range); input.oninput = () => update(input);
      button.onclick = async () => { if (await submit(value)) close(); };
      bindPending(root);
    });
  }
  function raiseSheet(c, op) {
    const h = c.table.hand, legal = h.legal_actions[op];
    let value = legal.min;
    showSheet(`
      <h3>${op === 'raise' ? '加注到' : '下注到'}多少？</h3>
      <div class="gc-note">金额是本轮下注的总额，不是再加多少。合法范围由服务端计算，不足完整加注的全下不会重新开放加注权。</div>
      <div class="gc-quick-sizes">${[[.5,'½ 池'],[.75,'¾ 池'],[1,'1 池'],[0,'最大']].map(([f,l]) => `
        <button class="btn ghost sm" data-gc-size="${f}">${l}</button>`).join('')}</div>
      <input class="gc-range" type="range" aria-label="加注到" min="${legal.min}" max="${legal.max}" step="1" value="${value}">
      <div class="gc-buy-value" data-gc-value>${num(value)}</div>
      <div class="gc-note">最小到 ${num(legal.min)} · 最大到 ${num(legal.max)}</div>
      <button class="btn block" data-gc-raise-confirm ${disabled(false)}>确认${op === 'raise' ? '加注' : '下注'}</button>
      <button class="link gc-close" data-cancel>取消</button>`, (root, close) => {
      const range = $('.gc-range', root);
      const update = v => { value = Math.max(legal.min, Math.min(legal.max, Math.round(v))); range.value = value; $('[data-gc-value]', root).textContent = num(value); };
      range.oninput = () => update(Number(range.value));
      $$('[data-gc-size]', root).forEach(b => b.onclick = () => update(Number(b.dataset.gcSize) ? (h.engine.current_bet || 0) + (h.engine.pot + (h.legal_actions.call?.amount || 0)) * Number(b.dataset.gcSize) : legal.max));
      $('[data-gc-raise-confirm]', root).onclick = async () => {
        if (c.table.hand?.turn !== h.turn || c.table.hand_no !== h.number) { close(); return toast('牌局已变化，请重新选择', true); }
        if (await action(c, op, { amount: value })) close();
      };
    });
  }
  function resultSheet(c, h, focusId = null, autoClose = false) {
    if (!h) return;
    clearTimeout(c.autoSheetTimer);
    clearInterval(c.sheetTicker);
    const t = c.table, result = h.result || {}, players = handPlayers(t, h), holdem = t.game === 'holdem';
    const isLatest = t.last_hand?.number === h.number;
    const canReveal = isLatest && !!t.last_hand?.can_reveal;
    const myRevealed = (h.revealed || []).includes(t.me.player);
    const focus = players.find(p => p.player === focusId && p.best5) || players.find(p => p.best5);
    const best = focus?.best5 || [];
    const playerName = id => esc(t.seats.find(s => s.player === id)?.name || '已离桌玩家');
    const pots = holdem ? `
      <div class="gc-note gc-payouts">${(result.pots || []).map((p, i) =>
        `${i ? '边池 ' + i : '主池'} ${num(p.amount)} → ${(p.winners || []).map(playerName).join('、')}`
      ).join('<br>')}${Object.entries(result.uncalled || {}).map(([id, amount]) =>
        `<br>未跟注退回：${playerName(id)} ${num(amount)}`
      ).join('')}</div>` : '';
    showSheet(`
      <div class="gc-result-head" data-gc-result-hand="${h.number}">${img('crown')}
        <h3>第 ${h.number} 手结算${autoClose ? ' <span class="gc-auto-close-pill" data-gc-sheet-sec>5s</span>' : ''}</h3>
        <span class="tiny muted">${holdem ? '' : esc({ last_player: '其他人都出局了 · ', all_in: '孤注一掷，全员比牌 · ', round_limit: '满 10 轮，统一比牌 · ' }[h.reason] || '')}默认不公开未赢摊牌底牌 · 不抽水</span>
      </div>
      ${holdem ? `
        <div class="gc-board">${(h.engine.board || []).map(code => card(code, 'sm', best)).join('')}</div>` : ''}${players.map(p => {
      const winner = result.winners?.includes(p.player), net = result.net?.[p.player];
      const isMe = p.player === t.me.player;
      const pubShown = p.shown || (h.revealed || []).includes(p.player);
      const subText = p.hand_name
        ? `${p.hand_name}${isMe && !pubShown ? ' · 仅自己可见' : pubShown ? ' · 已亮牌' : ''}`
        : (p.folded ? '已弃牌 · 默认不亮牌' : '默认不亮牌');
      return `
        <div class="gc-result-row ${winner ? 'winner' : ''}">
          <div class="grow">
            <button class="gc-result-player" ${p.best5 ? `data-gc-focus="${esc(p.player)}"` : 'disabled'}><b>${playerName(p.player)}${winner ? ' · 胜' : ''}${isMe ? '（我）' : ''}</b></button>
            <div class="tiny muted">${esc(subText)}${holdem && result.players?.[p.player]?.stack != null ? ' · 筹码 ' + num(result.players[p.player].stack) : ''}</div>
          </div>
          <div class="gc-result-cards">${cards(p.cards, holdem ? 2 : 3, 'xs', focus?.player === p.player ? best : [])}</div>
          <b class="${net >= 0 ? 'gc-up' : 'gc-down'}">${net == null ? '—' : signed(net)}</b>
        </div>`;
    }).join('')}${best.length ? `
        <div class="gc-center small">${playerName(focus.player)} · 最好的 5 张（点玩家切换）</div>
        <div class="gc-best-five">${best.map(code => card(code, 'sm', best)).join('')}</div>` : ''}
      ${canReveal ? `
        <button class="btn block gc-reveal-sheet-btn" type="button" data-gc-sheet-reveal="${h.number}">🃏 亮牌（向全桌展示本手底牌）</button>`
        : (isLatest && t.me.player && myRevealed ? '<div class="gc-center tiny muted" style="margin:6px 0">🃏 本手已向全桌亮牌</div>' : '')}
      ${pots}
      <div class="gc-note">${holdem ? '本手输赢留在桌上筹码中，离桌时才退回可用积分。' : '本手冻结已结算，没用完的部分已退回。'}<br>可用 ${num(funds(t.me.wallet, t))} ${unit(t)} · 已冻结 ${num(t.me.wallet.frozen)} 积分</div>
      ${holdem ? `
        <div class="gc-history-buttons">${c.history.map(x => `
          <button class="btn ghost sm" data-gc-hand="${x.number}">#${x.number}</button>`).join('')}</div>
        <div class="tiny muted">仅保留本次查看期间最近 10 手；重开页面只显示服务端返回的上一手。</div>` : ''}
      <button class="btn block" data-cancel>回到牌桌</button>`, (root, close) => {
      const stopAuto = () => {
        clearTimeout(c.autoSheetTimer);
        clearInterval(c.sheetTicker);
        const badge = $('[data-gc-sheet-sec]', root);
        if (badge) badge.remove();
      };
      if (autoClose) {
        let leftSec = Math.round(RESULT_SHOW_MS / 1000);
        c.sheetTicker = setInterval(() => {
          leftSec = Math.max(0, leftSec - 1);
          const badge = $('[data-gc-sheet-sec]', root);
          if (badge) badge.textContent = `${leftSec}s`;
        }, 1000);
        c.autoSheetTimer = setTimeout(() => { clearInterval(c.sheetTicker); close(); }, RESULT_SHOW_MS);
      }
      const revBtn = $('[data-gc-sheet-reveal]', root);
      if (revBtn) revBtn.onclick = async () => {
        stopAuto();
        revBtn.disabled = true;
        await tableWrite(c, 'reveal', { hand_no: Number(revBtn.dataset.gcSheetReveal) });
      };
      $$('[data-gc-hand]', root).forEach(b => b.onclick = () => { stopAuto(); resultSheet(c, c.history.find(x => x.number === Number(b.dataset.gcHand))); });
      $$('[data-gc-focus]', root).forEach(b => b.onclick = () => { stopAuto(); resultSheet(c, h, b.dataset.gcFocus); });
    });
  }
  function broadcast(t = null) {
    showSheet(`
      <h3>群播报怎么用？</h3>
      <p>管理员开启播报并选定授权群后，开桌会发送一张拉人卡。卡片显示游戏、场次、底注或盲注、房主昵称和人数，不显示任何人的余额。</p>
      <div class="gc-broadcast-card"><b>${t ? esc(names[t.game]) + ' · ' + esc(t.tier_name) : '会员 Bot · 开桌邀请'}</b>
        <div>${t ? esc(t.seats.find(s => s.player === t.host)?.name || '房主') + ` · ${t.seats.length}/${t.max_players} 人` : '人数随入座实时更新'}</div>
        <span>上桌 → 先看桌，再确认入座</span>
      </div>
      <div class="gc-note">首手开局或散桌后卡片会被删除。默认不开启播报；这里是说明，不表示本桌已经发卡。发送失败由服务端重试。你也可以把桌链接分享给朋友。</div>
      ${t?.deep_link ? '<button class="btn block" data-gc-share>分享桌链接</button>' : ''}
      <button class="link gc-close" data-cancel>知道了</button>`, root => {
      const b = $('[data-gc-share]', root);
      if (b) b.onclick = () => {
        const link = new URL(t.deep_link);
        if (link.protocol !== 'https:') return toast('桌链接无效', true);
        const url = `https://t.me/share/url?url=${encodeURIComponent(t.deep_link)}&text=${encodeURIComponent('来一起玩' + names[t.game])}`;
        if (tg?.openTelegramLink) tg.openTelegramLink(url);
        else navigator.clipboard?.writeText(t.deep_link).then(() => toast('桌链接已复制')).catch(() => toast('复制失败，请从 Telegram 分享', true));
      };
    });
  }
  function boardRules(game, config) {
    const total = Math.round((config.total_seconds || (game === 'xiangqi' ? 600 : 300)) / 60);
    const step = config.step_seconds || (game === 'xiangqi' ? 60 : 30);
    showSheet(`
      <h3>${names[game]} · 一看就懂</h3>
      <div class="gc-rule-body">${game === 'xiangqi' ? `
        <h4>怎么玩？</h4>
        <p>两人对弈，红方先走，轮流走一步。把对方的帅（将）将死就赢。自己的颜色总在下方。</p>
        <ul>
          <li><b>车：</b>横竖直走，格数不限。<b>马：</b>走"日"字，紧挨着的方向有棋子挡住（蹩马腿）就不能走。</li>
          <li><b>炮：</b>不吃子时和车一样走；吃子时中间必须隔一个棋子。</li>
          <li><b>相（象）：</b>走"田"字，田字中心有子（塞象眼）不能走，不能过河。<b>仕（士）：</b>在九宫里斜走一格。</li>
          <li><b>帅（将）：</b>在九宫里走一格；两边的帅和将不能在同一条直线上直接见面。</li>
          <li><b>兵（卒）：</b>过河前只能向前一格，过河后也可以左右走，但不能后退。</li>
        </ul>
        <h4>怎么算赢、算和？</h4>
        <p>将死对方，或者对方无子可走（困毙），就赢。反复将军（长将）的一方判负；同一局面重复三次判和；连续 60 回合没有吃子判和；双方都没有车、马、炮、兵也判和。随时可以认输，也可以向对方求和。</p>` : `
        <h4>怎么玩？</h4>
        <p>15×15 棋盘，黑方先下，轮流在交叉点落子。横、竖、斜任意一个方向连成五子（或更多）就赢，下满全盘算和棋。每局交换先后手。</p>
        <ul>
          <li><b>落子：</b>点棋盘选位置，会出现半透明的棋子；再点「落子」，或在同一位置再点一下确认。</li>
          <li><b>最后一手</b>会用红点标出；赢棋时，连成的五子会高亮。</li>
        </ul>`}
        <h4>时间</h4>
        <p>每方共 ${total} 分钟，每步最多 ${step} 秒，任一项用完判负。掉线时，轮到你的这一步放宽到 2 分钟，回来就能接着下。开局后先手方没在时间内走第一步，本局作废，押注退回。</p>
        <h4>积分</h4>
        <p>入座不扣分；开局时双方各冻结一份押注，赢家拿走两份，和棋或作废原额退回，不抽水。练习场陪机器人，只用欢乐豆，可以悔棋。</p>
      </div>
      <button class="btn block" data-cancel>知道了</button>`);
  }
  function rules(game = 'zjh', config = {}) {
    if (isBoard(game)) return boardRules(game, config);
    const zjh = game === 'zjh';
    showSheet(`
      <h3>${names[game]} · 一看就懂</h3>
      <div class="gc-rule-body">${zjh ? `
        <h4>怎么玩？</h4>
        <p>2–5 人，每人 3 张牌。开局先下底注，最后没弃牌的人比大小，赢家拿走底池。积分场只有真人，不抽水；练习场用欢乐豆，机器人陪玩。</p>
        <ul>
          <li><b>闷牌 / 看牌：</b>不看自己的牌叫闷牌；点自己的牌随时能看。看过后跟注要付双倍，不会重新计时。</li>
          <li><b>跟注 / 加注：</b>跟上当前注额才能继续；加注每次提高一档，最高到底注的 5 倍。</li>
          <li><b>弃牌：</b>这手不玩了，已经投入的不退。</li>
          <li><b>比牌：</b>第 2 轮起且看过牌才能选人比，要付跟注的两倍。一样大算发起的人输，牌面只给双方看。</li>
          <li><b>孤注一掷：</b>额度不够跟注时，把剩下的全投进去，所有还在局的人一起摊牌。最多 10 轮，到轮也统一摊牌。</li>
        </ul>
        <h4>哪种牌更大？</h4>
        <p>豹子（三张一样） &gt; 顺金（同花色连号） &gt; 金花（同花色） &gt; 顺子（连号） &gt; 对子 &gt; 单张。
          A23 是最小顺子，不比花色；最终摊牌完全一样就平分。</p>
        <p>235 吃豹子：${config.allow_235 ? '本桌开启。只有花色不同的 2、3、5 能赢豹子；同花 235 不算。没有豹子时，235 就是普通单张。' : '本桌关闭。'}</p>
        <h4>谁来开始？</h4>
        <p>房主加上已准备且余额够的玩家至少 2 人，房主就可以点开始；没准备的人本手旁观。至少 2 人且全员准备时，5 秒后自动开局；有人加入或取消准备，倒计时取消。每手后重新准备。</p>
        <h4>托管会替我怎么打？</h4>
        <p>每步默认 30 秒，超时会自动开启托管，也可以手动开关。第一次轮到先看牌：<b>有对子或更好就跟，单张就弃</b>；不加注，不主动比牌。
          跟不起时，有对子或更好才孤注一掷，否则弃牌。开启后约 1–2 秒替你行动，直到你取消。</p>
        <p>托管会自动准备下一手，最多连续 ${config.auto_hands || 3} 手（后台可调），之后自动离座，避免一直挂机输分。</p>
        <h4>积分怎么用？</h4>
        <p>入座不扣分；开局冻结单局封顶，结束马上结算并退回没花的部分。余额不够封顶就不能准备。至尊场每手冻结全部可用积分，<b>没有固定封顶</b>，请量力而行。</p>` : `
        <h4>怎么玩？</h4>
        <p>2–6 人，每人 2 张只有自己看到的底牌。桌面会陆续发出 5 张公共牌，大家从自己的 2 张和公共的 5 张里，挑最好的 5 张比大小。</p>
        <ul>
          <li><b>盲注：</b>每手庄家 D 轮转，小盲、大盲先自动放入一小笔筹码。两人桌庄家就是小盲，翻牌前先行动、之后后行动。有人离桌后，D 直接移到下一位在座玩家，偶尔会有人少交一次大盲。</li>
          <li><b>过牌：</b>不用补筹码，就先让下一人决定。<b>跟注：</b>补到和别人一样多。<b>弃牌：</b>放弃本手，已投的不退。</li>
          <li><b>下注 / 加注：</b>提高本轮要跟的总额。按钮里的“加注到”是本轮总额，不是再加多少。最小增加量等于上次完整加注增量，至少一个大盲。</li>
          <li><b>全下：</b>把剩下筹码都投入。筹码少也能玩，只能赢自己有份的主池/边池；没人跟的部分退回。不足一次完整加注的全下不会重新开放别人的加注权。</li>
          <li>依次是翻牌前、翻牌（3 张）、转牌（第 4 张）、河牌（第 5 张）。都不弃牌就摊牌比大小。</li>
        </ul>
        <h4>牌型从大到小</h4>
        <p>皇家同花顺 &gt; 同花顺 &gt; 四条 &gt; 葫芦 &gt; 同花 &gt; 顺子 &gt; 三条 &gt; 两对 &gt; 一对 &gt; 高牌。平分的零头给 D 左侧最先的赢家。</p>
        <h4>开局、暂离和连续发牌</h4>
        <p>首手由房主开始：至少 2 人有筹码且没暂离；坐满 6 人自动开始。之后每手结束约 4 秒自动发下一手，够 2 人就继续。新入座从下一手参加；暂离也从下一手生效，跳过你、不扣盲注。</p>
        <h4>托管会替我怎么打？</h4>
        <p>每步默认 30 秒，超时自动开启托管。<b>能免费过牌就过牌</b>。需要跟注时，有一对或更好，而且这次跟注不超过当前底池一半才跟，否则弃牌。还没翻公共牌时，手里是对子，或两张都是 A/K/Q，才算符合成牌条件。</p>
        <p>如果跟注就会花光筹码，要有两对或更好才跟。不主动下注、不加注、不主动全下；约 1–2 秒自动出手，直到你取消。
          连续托管 ${config.auto_hands || 10} 手或 ${Math.round((config.auto_seconds || 600) / 60)} 分钟会自动暂离；
          暂离 ${Math.round((config.away_seconds || 600) / 60)} 分钟会离桌结算（后台可调）。</p>
        <h4>带入和补码</h4>
        <p>确认带入后冻结为桌上筹码。两手之间可补码，不超过最高带入；至尊场没有带入上限，但不能超过可用积分。输赢在桌内转移，不抽水、不增发；离桌退回剩余。积分流水只记带入、补码和离桌结算。</p>`}
        <h4>欢乐豆、掉线和公平</h4>
        <p>每天补足到 ${num(wallet?.daily_beans || 10000)} 欢乐豆，仅供练习，不能换积分。切走页面或掉线不暂停服务端计时，重新打开会找回原桌。房主离开或等待时 3 分钟未操作会移交；10 分钟没人操作会散桌。</p>
        <p>别人没公开的手牌不会发到你的设备上。</p>
      </div>
      <button class="btn block" data-cancel>知道了</button>`);
  }

  return { lobby, table, activeTable, dispose };
};

