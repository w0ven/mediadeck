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
  // Sound is decoration: browsers allow it only after a tap, and a missing file never blocks play.
  const sound = (() => {
    const base = '/app/assets/game-assets/audio/', buffers = new Map();
    let ctx = null, track = null, wanted = false;
    const pref = kind => { try { return localStorage.getItem('gc-sound-' + kind) !== 'off'; } catch { return true; } };
    function sync() {
      const on = wanted && pref('music') && document.visibilityState === 'visible';
      if (on && !track) { track = new Audio(base + 'music.mp3'); track.loop = true; track.volume = .5; }
      if (on) track.play().catch(() => {});
      else track?.pause();
    }
    function unlock() {
      const Context = window.AudioContext || window.webkitAudioContext;
      if (!ctx && Context) ctx = new Context();
      if (ctx?.state === 'suspended') ctx.resume().catch(() => {});
      sync();
    }
    function load(name) {
      if (!buffers.has(name)) buffers.set(name, fetch(base + name + '.mp3')
        .then(r => r.ok ? r.arrayBuffer() : Promise.reject(new Error(String(r.status))))
        .then(data => new Promise((ok, fail) => ctx.decodeAudioData(data, ok, fail))).catch(() => null));
      return buffers.get(name);
    }
    async function play(name, volume = 1) {
      if (!ctx || ctx.state !== 'running' || !pref('sfx')) return;
      const buffer = await load(name);
      if (!buffer || ctx.state !== 'running') return;
      const source = ctx.createBufferSource(), gain = ctx.createGain();
      source.buffer = buffer; gain.gain.value = volume;
      source.connect(gain).connect(ctx.destination);
      source.start();
    }
    function set(kind, on) {
      try { localStorage.setItem('gc-sound-' + kind, on ? 'on' : 'off'); } catch { /* private mode keeps defaults */ }
      sync();
    }
    document.addEventListener('pointerdown', unlock, true);
    document.addEventListener('visibilitychange', sync);
    return { play, pref, set, music(on) { wanted = on; sync(); } };
  })();

  function dispose() {
    if (current) {
      current.disposed = true;
      stopStream(current);
      clearInterval(current.clock);
      clearTimeout(current.celebrateTimer);
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
    };
    $('#screen').classList.add('gc-scene', 'gc-table-screen');
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
      ...p, cards: p.hole, folded: p.status === 'folded', all_in: p.status === 'all_in',
      invested: p.committed, hand_name: result?.players?.[p.player]?.hand,
      best5: result?.players?.[p.player]?.shown ? result.players[p.player].best5 : null,
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
    if (t.last_hand && !c.history.some(h => h.number === t.last_hand.number)) c.history.unshift(t.last_hand);
    c.history = c.history.slice(0, 10);
    react(c, previous, t);
    paintTable(c);
    if (t.phase === 'closed') { stopStream(c); streamStatus(c, 'paused'); }
    const me = t.seats.find(s => s.player === t.me.player);
    const notice = t.me.notice;
    if (notice && notice !== c.notice && me?.auto && notice.includes('超时')) {
      c.notice = notice;
      timeoutSheet(c);
    } else if (notice && notice !== c.notice) { c.notice = notice; toast(notice, true); }
    if (!notice) c.notice = '';
  }
  // Finished hands stay on the felt briefly (winner, nets, shown cards) before any result sheet.
  function react(c, previous, t) {
    const ph = previous?.hand, fx = events(previous, t);
    const loud = ['allin', 'raise', 'chip', 'fold', 'check', 'look'].find(k => fx.some(e => e.kind === k));
    if (loud) sound.play({ look: 'flip', chip: 'chip' }[loud] || loud);
    if (t.hand && t.hand.number !== ph?.number) {
      c.celebrate = null;
      c.dealAt = performance.now();
      if (previous && !isBoard(t.game)) sound.play('deal');
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
    if (t.me.player && t.hand?.current_player === t.me.player
      && (ph?.current_player !== t.me.player || ph?.number !== t.hand.number)) sound.play('turn');
    if (ph && t.last_hand?.number === ph.number && t.hand?.number !== ph.number) {
      const h = t.last_hand, net = h.result?.net || {}, motion = !matchMedia('(prefers-reduced-motion: reduce)').matches;
      clearTimeout(c.celebrateTimer);
      if (motion) {
        c.celebrate = { hand: h, at: performance.now() };
        fx.push(...Object.entries(net).filter(([, n]) => n).map(([player, n]) =>
          ({ player, kind: n > 0 ? 'win' : 'loss', label: signed(n), ms: CELEBRATE_MS })));
      }
      if (net[t.me.player] > 0) sound.play('win');
      else if (net[t.me.player] < 0) sound.play('lose');
      c.celebrateTimer = setTimeout(() => {
        if (!live(c) || motion && c.celebrate?.hand !== h) return;
        if (motion) { c.celebrate = null; paintTable(c); }
        const me = c.table.seats.find(s => s.player === c.table.me.player);
        if (c.table.game === 'zjh' && !c.table.hand && !me?.auto && !$('.gc-sheet')) resultSheet(c, h);
      }, motion ? CELEBRATE_MS : 0);
    }
    if (fx.length) pushFx(c, fx);
  }
  const celebrating = c => !c.table.hand && c.celebrate && performance.now() - c.celebrate.at < CELEBRATE_MS ? c.celebrate.hand : null;
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
  const FX_MS = 1700, CELEBRATE_MS = 2800;
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
      const age = at - f.at, spot = seatPoint(c, f.player);
      if (age >= (f.ms || FX_MS) || !spot) return '';
      const delay = `animation-delay:-${Math.round(age)}ms`;
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
  function card(code, size = '', best = []) {
    if (!Number.isInteger(code) || code < 0 || code > 51) return `<span class="gc-card gc-card-back ${size}" aria-label="未公开的牌">${img('card-back')}</span>`;
    const s = Math.floor(code / 13), suit = ['♠', '♥', '♣', '♦'][s], rank = ranks[code % 13];
    return `<span class="gc-card ${s === 1 || s === 3 ? 'red' : ''} ${size} ${best.includes(code) ? 'gc-best' : ''}" aria-label="${['黑桃','红桃','梅花','方块'][s]}${rank}"><span class="gc-card-rank">${rank}<i>${suit}</i></span><span class="gc-card-suit">${suit}</span></span>`;
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
  function seatHtml(seat, pos, t, hand = t.hand) {
    if (!seat) return `
      <div class="gc-seat gc-pos${pos} gc-empty-seat">
        <div class="gc-avatar-ring">＋</div>
        <span>空位</span>
      </div>`;
    const p = handPlayers(t, hand).find(p => p.player === seat.player);
    const active = t.hand?.current_player === seat.player;
    const won = !t.hand && hand?.result?.winners?.includes(seat.player);
    return `
      <div class="gc-seat gc-pos${pos} ${p?.folded ? 'gc-folded' : ''} ${won ? 'gc-winner' : ''}" data-gc-player="${esc(seat.player)}">
        <div class="gc-avatar-ring ${active ? 'gc-active' : ''}" ${active ? 'data-gc-ring' : ''}>
          ${avatar(seat, t)}${markers(seat, t)}
          <span class="gc-seat-status ${seat.auto ? 'gc-auto-status' : seat.ready ? 'gc-ready-status' : ''}">${seatStatus(seat, t, p)}</span>
        </div>
        <div class="gc-seat-name">${esc(seat.name)}</div>
        <div class="gc-seat-tags">${seat.player === t.host ? '<span class="gc-host">房主</span>' : ''}${label(seat, t)}</div>
        ${titlePlaque(seat, t)}${t.game === 'holdem' ? `
          <div class="gc-stack num">${num(p?.stack ?? seat.stack)}</div>` : ''}${p ? `
          <div class="gc-mini-cards">${cards(p.cards, t.game === 'zjh' ? 3 : 2, 'xs')}</div>
          <span class="gc-bet num">${img('chip')} ${num(p.invested ?? p.bet)}</span>` : ''}
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
  function celebration(t, h) {
    const winners = h.result?.winners || [];
    const who = winners.map(id => t.seats.find(s => s.player === id)?.name || '已离桌玩家').join('、');
    const won = Math.max(0, ...winners.map(id => h.result?.net?.[id] || 0));
    const why = t.game === 'holdem' ? h.engine?.street === 'showdown' ? '摊牌比大小' : '其他人都弃牌了'
      : { last_player: '其他人都出局了', all_in: '孤注一掷 · 全员比牌', round_limit: '满 10 轮 · 统一比牌' }[h.reason] || '比牌结束';
    const board = t.game === 'holdem' ? `<div class="gc-board">${(h.engine?.board || []).map(code => card(code, 'sm')).join('')}</div>` : '';
    return `
      <div class="${t.game === 'holdem' ? 'gc-holdem-center' : 'gc-pot'} gc-celebrate">${board}
        ${img('crown', 'gc-celebrate-crown')}
        <div class="gc-celebrate-name">${esc(who || '本手')} 赢了</div>
        <div class="gc-pot-value num">+${num(won)}</div>
        <div class="small">${why}</div>
      </div>`;
  }
  function tableCenter(t, shown = null) {
    if (shown) return celebration(t, shown);
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
      return `
        <div class="gc-holdem-center">
          <div class="tiny muted">第 ${h.number} 手 · ${esc({ preflop: '翻牌前', flop: '翻牌', turn: '转牌', river: '河牌', showdown: '摊牌' }[e.street] || '进行中')}</div>
          <div class="gc-board">${Array.from({ length: 5 }, (_, i) => i < (e.board || []).length ? card(e.board[i], 'sm') : '<span class="gc-card-slot"></span>').join('')}</div>
          <div class="gc-pot-value">${img('chip-stack')} ${num(e.pot)}</div>
          <div class="gc-pots">${(e.pots || []).map((p, i) => `<span>${i ? '边池 ' + i : '主池'} ${num(p.amount)}</span>`).join('')}</div>
          <span class="gc-ticker">${t.seats.find(s => s.player === h.current_player)?.name ? esc(t.seats.find(s => s.player === h.current_player).name) + ' 行动中' : '等待服务端发牌'}</span>
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
  function controls(t, me) {
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
          : t.countdown_at ? '<span data-gc-countdown>4</span> 秒后继续发牌' : esc(state.reason)}</div>`;
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
    return `
      ${actions.length ? `
        <div class="gc-actions">${actions.map(a => `
          <button class="gc-action ${['call', 'check'].includes(a) ? 'main'
            : ['raise', 'bet'].includes(a) ? 'alt' : ['compare', 'all_in'].includes(a) ? 'warm' : ''}"
            data-gc-action="${esc(a)}" ${disabled(false)}>
            ${esc(labels[a] || a)}
            <small>${legal[a].amount != null ? num(legal[a].amount)
              : legal[a].min != null ? '最小到 ' + num(legal[a].min) : a === 'fold' ? '放弃本手' : '按规则执行'}</small>
          </button>`).join('')}</div>` : `
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
    if (!h) return outcome + controls(t, me);
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
    const shown = celebrating(c), view = t.hand || shown;
    const p = handPlayers(t, view).find(p => p.player === bottom?.player);
    const dealAge = c.dealAt ? performance.now() - c.dealAt : Infinity;
    const look = !!t.hand?.legal_actions?.look;
    const timeout = me?.auto && t.me.notice?.includes('超时');
    const timeoutHtml = timeout ? `
      <div class="gc-timeout" role="alert">已超时，进入托管
        <button class="btn sm" data-gc-takeover>取消托管</button>
      </div>` : '';
    const seats = Array.from({ length: t.max_players - 1 }, (_, i) =>
      seatHtml(t.seats.find(s => s.seat === (anchor + i + 1) % t.max_players), i + 1, t, view)).join('');
    const oldScroll = $('#screen').scrollTop;
    setScreen(`
      <section class="gc gc-table ${t.game === 'holdem' ? 'gc-holdem' : 'gc-zjh'} gc-felt-${Object.hasOwn(tierClasses, t.tier) ? t.tier : 'practice'} ${dealAge < 1200 ? 'gc-dealing' : ''}"
        style="--deal-age:-${Math.round(Math.min(dealAge, 1200))}ms" data-table-id="${esc(t.id)}" data-revision="${t.revision}" data-phase="${esc(t.phase)}">
        <header class="gc-table-bar">
          <button class="gc-tool" data-gc-back>‹ 返回</button>
          <div class="grow"><b>${esc(names[t.game])} <span class="gc-tier-chip ${tierClasses[t.tier] || ''}">${esc(t.tier_name)}</span></b>
            <div class="tiny muted">${t.game === 'zjh' ? `底注 ${num(t.rules.ante)} · ${t.rules.cap == null ? '不封顶' : '封顶 ' + num(t.rules.cap)}` : `无限注 · 盲注 ${num(t.rules.small_blind)}/${num(t.rules.big_blind)}`} · 不抽水</div>
          </div>
          <button class="gc-tool gc-sound-btn" data-gc-sound aria-label="声音">${icon(sound.pref('sfx') || sound.pref('music') ? 'volume-2' : 'volume-x')}</button>
          <button class="gc-tool" data-gc-rules>玩法</button>
        </header>
        <div class="gc-tools">
          <button class="gc-auto ${me?.auto ? 'on' : ''}" role="switch" aria-checked="${!!me?.auto}" aria-label="托管" data-gc-auto ${disabled(!me || me.leave_after)}><i></i>${me?.auto ? '取消托管' : '托管'}</button>
          ${t.game === 'holdem' ? `
            <button class="gc-tool ${me?.away ? 'on' : ''}" data-gc-away ${disabled(!me || me.leave_after)}>暂离${me?.away ? '中' : ''}</button>
            <button class="gc-tool" data-gc-topup ${disabled(!me || me.leave_after || t.phase === 'playing')}>补码</button>` : ''}
          <button class="link" data-gc-history ${c.history.length ? '' : 'disabled'}>${t.game === 'holdem' ? '最近手牌' : '上手结算'}</button>
          <button class="link" data-gc-leave ${disabled(!me)}>离桌</button>
        </div>
        <div class="gc-connection-row">
          <button class="gc-connection" data-gc-connection data-state="${c.status}"></button>
          ${timeout && t.game === 'holdem' ? timeoutHtml
            : t.currency === 'beans' ? '<span>练习场 · 机器人陪玩</span>' : '<button class="link" data-gc-broadcast>群播报说明</button>'}
        </div>
        ${t.game === 'zjh' ? timeoutHtml : ''}
        <div data-gc-retry-slot>${pendingHtml()}</div>
        <div class="gc-felt">${croupier(t)}
          <div class="gc-oval"></div>
          ${seats}${tableCenter(t, shown)}
        </div>
        <div class="gc-dock">${bottom ? `
          <div class="gc-me-row">
            <div class="gc-avatar-ring ${t.hand?.current_player === bottom.player ? 'gc-active' : ''}"
              ${t.hand?.current_player === bottom.player ? 'data-gc-ring' : ''}>
              ${avatar(bottom, t)}${markers(bottom, t)}
              <span class="gc-seat-status ${bottom.auto ? 'gc-auto-status' : ''}">${seatStatus(bottom, t, p)}</span>
            </div>
            <div class="grow">
              <div class="gc-me-name">
                ${esc(bottom.name)}${bottom.player === t.host ? '<span class="gc-host">房主</span>' : ''}${label(bottom, t)}
                ${p ? `<span class="gc-bet num">${img('chip')} ${num(t.game === 'holdem' ? p.bet : p.invested)}</span>` : ''}
              </div>
              ${titlePlaque(bottom, t)}
              <div class="tiny muted">${me ? `${t.game === 'holdem' ? '桌上筹码 ' + num(p?.stack ?? me.stack) + ' · ' : ''}可用 ${num(funds(t.me.wallet, t))} ${unit(t)} · 已冻结 ${num(t.me.wallet.frozen)} 积分` : '旁观视角 · 不展示未公开手牌'}</div>
            </div>
          </div>` : ''}
      ${view ? `
          <button class="gc-hand" aria-label="${look ? '看牌' : '自己的手牌'}" ${look ? `data-gc-action="look" ${disabled(false)}` : 'disabled'}>
            ${p ? cards(p.cards, t.game === 'zjh' ? 3 : 2, 'lg') : '<span class="muted">本手旁观，从下一手加入</span>'}
          </button>
          <div class="gc-hand-label">${shown ? esc(shownLabel(shown, p, t)) : look ? '点自己的牌，随时看牌' : esc(p?.hand_name || (p?.folded ? '已弃牌' : p?.seen ? '已看牌' : t.game === 'zjh' ? '闷牌中' : '两张底牌 + 五张公共牌，选最好的五张'))}</div>` : `
          <div class="gc-wait-message">${t.last_hand ? `
            <button class="gc-result-peek" data-gc-history>
              ${img('crown')} 第 ${t.last_hand.number} 手已结算${t.last_hand.result?.net?.[t.me.player] != null
                ? ' · 本手 ' + signed(t.last_hand.result.net[t.me.player]) : ''} <span>查看 ›</span>
            </button>` : `${img('emblem-' + t.game)}
            <span>${me ? t.game === 'holdem' ? '首手房主开始，满 6 人自动开局' : '房主开始或全员准备后开局' : '先看桌，再决定是否入座'}
              <small>${t.game === 'zjh' ? '入座不扣分 · 每步 30 秒' : '首手房主开始 · 之后连续发牌'}</small>
            </span>`}</div>`}
      ${controls(t, me)}${t.hand?.comparisons?.length ? '<button class="link gc-close" data-gc-comparisons>查看仅比牌双方可见的结果</button>' : ''}</div>
      </section>`);
    $('#screen').scrollTop = oldScroll;
    bindTable(c);
    tick(c);
    streamStatus(c, c.status);
    drawFx(c);
  }
  function shownLabel(h, p, t) {
    const net = h.result?.net?.[t.me.player];
    const result = net > 0 ? `本手赢 ${num(net)}` : net < 0 ? `本手输 ${num(-net)}` : '';
    return [p?.hand_name, result].filter(Boolean).join(' · ') || '本手已结束';
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
      sound.play('tick', .7);
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
      if (t.game === 'holdem' && ['raise', 'bet'].includes(op)) return raiseSheet(c, op);
      if (op === 'all_in') return confirmAction(c, op);
      action(c, op);
    });
    const comparison = $('[data-gc-comparisons]', root);
    if (comparison) comparison.onclick = () => comparisonSheet(c);
    bindPending(root);
  }
  function soundSheet() {
    const row = (kind, title, note) => `
      <button class="gc-switch-row" role="switch" aria-checked="${sound.pref(kind)}" data-gc-sound-kind="${kind}">
        <span><b>${title}</b><small>${note}</small></span><i></i>
      </button>`;
    showSheet(`
      <h3>声音</h3>
      ${row('music', '背景音乐', '只在牌桌播放，切到后台自动暂停')}
      ${row('sfx', '音效', '发牌、下注、弃牌、轮到你和输赢提示')}
      <button class="btn block" data-cancel>好的</button>`, root => {
      $$('[data-gc-sound-kind]', root).forEach(b => b.onclick = () => {
        const on = b.getAttribute('aria-checked') !== 'true';
        sound.set(b.dataset.gcSoundKind, on);
        b.setAttribute('aria-checked', String(on));
        if (on && b.dataset.gcSoundKind === 'sfx') sound.play('tap');
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
  function resultSheet(c, h, focusId = null) {
    if (!h) return;
    const t = c.table, result = h.result || {}, players = handPlayers(t, h), holdem = t.game === 'holdem';
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
      <div class="gc-result-head">${img('crown')}
        <h3>第 ${h.number} 手结算</h3>
        <span class="tiny muted">${holdem ? '' : esc({ last_player: '其他人都出局了 · ', all_in: '孤注一掷，全员比牌 · ', round_limit: '满 10 轮，统一比牌 · ' }[h.reason] || '')}不抽水 · ${holdem ? '牌桌仍会连续发牌' : '继续玩需重新准备'}</span>
      </div>
      ${holdem ? `
        <div class="gc-board">${(h.engine.board || []).map(code => card(code, 'sm', best)).join('')}</div>` : ''}${players.map(p => {
      const winner = result.winners?.includes(p.player), net = result.net?.[p.player];
      return `
        <div class="gc-result-row ${winner ? 'winner' : ''}">
          <div class="grow">
            <button class="gc-result-player" ${p.best5 ? `data-gc-focus="${esc(p.player)}"` : 'disabled'}><b>${playerName(p.player)}${winner ? ' · 胜' : ''}</b></button>
            <div class="tiny muted">${esc(p.hand_name || (p.folded ? '已弃牌 · 牌面不公开' : '未公开牌型'))}${holdem && result.players?.[p.player]?.stack != null ? ' · 筹码 ' + num(result.players[p.player].stack) : ''}</div>
          </div>
          <div class="gc-result-cards">${cards(p.cards, holdem ? 2 : 3, 'xs', focus?.player === p.player ? best : [])}</div>
          <b class="${net >= 0 ? 'gc-up' : 'gc-down'}">${net == null ? '—' : signed(net)}</b>
        </div>`;
    }).join('')}${best.length ? `
        <div class="gc-center small">${playerName(focus.player)} · 最好的 5 张（点玩家切换）</div>
        <div class="gc-best-five">${best.map(code => card(code, 'sm', best)).join('')}</div>` : ''}${pots}
      <div class="gc-note">${holdem ? '本手输赢留在桌上筹码中，离桌时才退回可用积分。' : '本手冻结已结算，没用完的部分已退回。'}<br>可用 ${num(funds(t.me.wallet, t))} ${unit(t)} · 已冻结 ${num(t.me.wallet.frozen)} 积分</div>
      ${holdem ? `
        <div class="gc-history-buttons">${c.history.map(x => `
          <button class="btn ghost sm" data-gc-hand="${x.number}">#${x.number}</button>`).join('')}</div>
        <div class="tiny muted">仅保留本次查看期间最近 10 手；重开页面只显示服务端返回的上一手。</div>` : ''}
      <button class="btn block" data-cancel>回到牌桌</button>`, root => {
      $$('[data-gc-hand]', root).forEach(b => b.onclick = () => resultSheet(c, c.history.find(x => x.number === Number(b.dataset.gcHand))));
      $$('[data-gc-focus]', root).forEach(b => b.onclick = () => resultSheet(c, h, b.dataset.gcFocus));
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

