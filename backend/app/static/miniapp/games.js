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
  const img = (key, cls = '', alt = '') => `<img class="gc-art ${cls}" src="/app/assets/game-assets/${key}.webp" alt="${esc(alt)}">`;
  const unit = t => t.currency === 'beans' ? '欢乐豆' : '积分';
  const funds = (w, t) => Number(w?.[t.currency === 'beans' ? 'beans' : 'points'] || 0);
  const reqId = () => crypto.randomUUID ? crypto.randomUUID()
    : Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
  const get = path => api(ROOT + path);
  let catalog = null, wallet = null, current = null, busy = false, pending = null;
  let sheetGeneration = 0;
  const tierCache = new Map();

  function dispose() {
    if (current) {
      current.disposed = true;
      stopStream(current);
      clearInterval(current.clock);
      current = null;
    }
    $('#screen').classList.remove('gc-table-screen');
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
    const soon = ['niuniu', 'doudizhu', 'gomoku', 'xiangqi'].map((id, i) => `
      <button class="gc-game-card" data-gc-soon>${img('emblem-' + id)}<span><b>${names[id]}</b><span class="tiny muted">${['坐庄比牌', '三人对战', '1v1 对弈', '1v1 对弈'][i]}</span><em>即将上线</em></span></button>`).join('');
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
    const lists = await Promise.all(['zjh', 'holdem'].map(g => loadTiers(g).catch(() => [])));
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
    if (game === 'holdem') return `盲注 ${num(t.small_blind)}/${num(t.big_blind)} · 带入 ${num(t.buy_min)}${t.buy_max == null ? ' 起，不设上限' : '–' + num(t.buy_max)}`;
    return `底注 ${num(t.ante)} · ${t.cap == null ? '单局不封顶' : '封顶 ' + num(t.cap)}`;
  }
  async function tiers(game, alive) {
    const [ts, w, c] = await Promise.all([loadTiers(game), get('/wallet'), get('/lobby')]);
    if (!alive()) return;
    wallet = w; catalog = c;
    const g = c.games.find(x => x.id === game);
    const available = c.enabled && g?.enabled && g?.available;
    const rows = ts.map(t => {
      const short = funds(w, t) < t.min_balance;
      return `
        <article class="gc-tier ${tierClasses[t.id] || ''}" data-gc-tier="${esc(t.id)}">
          <div class="gc-tier-badge">${img('tier-' + t.id)}<b>${esc(t.name.replace('场', ''))}<small>${t.id === 'supreme' ? '无上限' : t.currency === 'beans' ? '欢乐豆' : ''}</small></b></div>
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
  function openTable(id, snapshot = null) { push(alive => table(id, alive, snapshot)); }
  const live = c => current === c && !c.disposed && c.alive();
  async function table(id, alive, initial = null) {
    const data = initial ? { table: initial } : await get('/tables/' + encodeURIComponent(id));
    if (!alive()) return;
    const c = current = {
      id, alive, table: null, disposed: false, stream: null,
      generation: 0, attempt: 0, status: 'connecting', offset: 0, notice: '', history: [],
    };
    $('#screen').classList.add('gc-table-screen');
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
    if (!c.commitments) c.commitments = new Map();
    if (t.hand && !c.commitments.has(t.hand.number)) c.commitments.set(t.hand.number, t.hand.commitment);
    c.offset = Number(t.server_time) - Date.now() / 1000;
    c.table = t; wallet = t.me.wallet;
    if (t.last_hand && !c.history.some(h => h.number === t.last_hand.number)) c.history.unshift(t.last_hand);
    c.history = c.history.slice(0, 10);
    paintTable(c);
    if (t.phase === 'closed') { stopStream(c); streamStatus(c, 'paused'); }
    const me = t.seats.find(s => s.player === t.me.player);
    const notice = t.me.notice;
    if (notice && notice !== c.notice && me?.auto && notice.includes('超时')) {
      c.notice = notice;
      timeoutSheet(c);
    } else if (notice && notice !== c.notice) { c.notice = notice; toast(notice, true); }
    if (!notice) c.notice = '';
    if (previous?.phase === 'playing' && t.last_hand && t.game === 'zjh' && !me?.auto && !$('.gc-sheet')) resultSheet(c, t.last_hand);
  }
  function tagOf(seat, t) {
    // Public profile tags are optional API fields; never invent another member's level/title.
    return seat.level_tag || (seat.player === t.me.player ? myTag() : null);
  }
  function avatar(seat, t, cls = '') {
    const tag = tagOf(seat, t);
    return `
      <div class="avatar ${cls} ${esc(lvClass(tag))} ${seat.bot ? 'gc-bot' : ''}">${seat.bot ? '🤖' : esc(Array.from(seat.name || '?')[0])}</div>`;
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
  function seatHtml(seat, pos, t) {
    if (!seat) return `
      <div class="gc-seat gc-pos${pos} gc-empty-seat">
        <div class="gc-avatar-ring">＋</div>
        <span>空位</span>
      </div>`;
    const p = handPlayers(t).find(p => p.player === seat.player);
    const active = t.hand?.current_player === seat.player;
    return `
      <div class="gc-seat gc-pos${pos} ${p?.folded ? 'gc-folded' : ''}" data-gc-player="${esc(seat.player)}">
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
    const eligible = t.game === 'zjh' ? t.seats.filter(s => (s.ready || s.player === t.host) && (s.player !== t.me.player || enough)).length : t.seats.filter(s => !s.away && s.stack > 0).length;
    const missing = Math.max(0, 2 - eligible);
    let reason = missing ? `还差 ${missing} 人${t.game === 'zjh' ? '准备' : '入座并带入'}可以开始` : '人数已够，房主可以开始';
    if (t.game === 'zjh' && t.me.is_host && !enough) reason = `你需 ${num(need)} 可用${unit(t)}才能参与；${eligible >= 2 ? '其他人已够开局，你本手旁观' : `还差 ${missing} 人可以开始`}`;
    if (t.game === 'holdem' && t.hand_no > 0) {
      const human = t.seats.find(s => !s.bot);
      reason = t.currency === 'beans' && (human?.away || !human?.stack)
        ? '练习桌已暂停；恢复入局或补码后继续'
        : missing ? '等待至少 2 人有筹码且未暂离' : '等待下一手自动发牌';
    }
    return { eligible, enough, can: eligible >= 2, reason, host };
  }
  function croupier(t) {
    const tier = Object.hasOwn(tierClasses, t.tier) ? t.tier : 'practice';
    return `
      <div class="gc-croupier" aria-hidden="true"><span class="gc-croupier-silhouette"></span><img src="/app/assets/game-assets/dealer-${tier}.webp" alt="" onload="this.parentElement.classList.add('has-art')" onerror="this.remove()"></div>`;
  }
  function tableCenter(t) {
    if (!t.hand) {
      const s = startState(t);
      return `
        <div class="gc-pot gc-wait-center">
          <div class="tiny muted">${t.phase === 'closed' ? '本桌已结束' : t.phase === 'between' ? '准备下一手' : '等待开局'}</div>
          <b>${t.seats.length}<small>/${t.max_players}</small></b>
          <div class="small">${t.countdown_at ? '<span data-gc-countdown>5</span> 秒后自动开局' : t.phase === 'closed' ? '积分已按结算退回' : esc(s.reason)}</div>
          <div class="tiny muted">${t.phase !== 'closed' ? '房主 ' + esc(s.host?.name || '—') : ''}</div>
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
        <div class="gc-actions">${t.game === 'zjh' ? `
          <button class="gc-action ${me.ready ? '' : 'main'}" data-gc-ready ${disabled(!me.ready && !state.enough)}>
            ${me.ready ? '取消准备' : !state.enough ? '余额不足' : '准备'}
            <small>${me.ready ? '等待房主开始' : '每手需重新准备'}</small>
          </button>` : ''}${t.me.is_host ? `
          <button class="gc-action main" data-gc-start ${disabled(!state.can)}>开始
            <small>${state.can ? t.game === 'holdem' ? '有筹码且未暂离者参加' : '未准备的人本手旁观' : '条件尚未满足'}</small>
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
  function paintTable(c) {
    if (!live(c)) return;
    const t = c.table, me = t.seats.find(s => s.player === t.me.player);
    const anchor = me?.seat ?? 0;
    const bottom = me || t.seats.find(s => s.seat === anchor);
    const p = handPlayers(t).find(p => p.player === bottom?.player);
    const look = !!t.hand?.legal_actions?.look;
    const timeout = me?.auto && t.me.notice?.includes('超时');
    const timeoutHtml = timeout ? `
      <div class="gc-timeout" role="alert">已超时，进入托管
        <button class="btn sm" data-gc-takeover>取消托管</button>
      </div>` : '';
    const displayHand = t.hand || t.last_hand;
    const seats = Array.from({ length: t.max_players - 1 }, (_, i) => seatHtml(t.seats.find(s => s.seat === (anchor + i + 1) % t.max_players), i + 1, t)).join('');
    const oldScroll = $('#screen').scrollTop;
    setScreen(`
      <section class="gc gc-table ${t.game === 'holdem' ? 'gc-holdem' : 'gc-zjh'}" data-table-id="${esc(t.id)}" data-revision="${t.revision}" data-phase="${esc(t.phase)}">
        <header class="gc-table-bar">
          <button class="gc-tool" data-gc-back>‹ 返回</button>
          <div class="grow"><b>${esc(names[t.game])} <span class="gc-tier-chip ${tierClasses[t.tier] || ''}">${esc(t.tier_name)}</span></b>
            <div class="tiny muted">${t.game === 'zjh' ? `底注 ${num(t.rules.ante)} · ${t.rules.cap == null ? '不封顶' : '封顶 ' + num(t.rules.cap)}` : `无限注 · 盲注 ${num(t.rules.small_blind)}/${num(t.rules.big_blind)}`} · 不抽水</div>
          </div>
          <button class="gc-tool" data-gc-verify ${displayHand ? '' : 'disabled'}>${icon('shield-check')}复核</button>
        </header>
        <div class="gc-tools">
          <button class="gc-auto ${me?.auto ? 'on' : ''}" role="switch" aria-checked="${!!me?.auto}" aria-label="托管" data-gc-auto ${disabled(!me || me.leave_after)}><i></i>${me?.auto ? '取消托管' : '托管'}</button>
          ${t.game === 'holdem' ? `
            <button class="gc-tool ${me?.away ? 'on' : ''}" data-gc-away ${disabled(!me || me.leave_after)}>暂离${me?.away ? '中' : ''}</button>
            <button class="gc-tool" data-gc-topup ${disabled(!me || me.leave_after || t.phase === 'playing')}>补码</button>` : ''}
          <button class="link" data-gc-rules>玩法</button>
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
          ${seats}${tableCenter(t)}
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
      ${t.hand ? `
          <button class="gc-hand" aria-label="${look ? '看牌' : '自己的手牌'}" ${look ? `data-gc-action="look" ${disabled(false)}` : 'disabled'}>
            ${p ? cards(p.cards, t.game === 'zjh' ? 3 : 2, 'lg') : '<span class="muted">本手旁观，从下一手加入</span>'}
          </button>
          <div class="gc-hand-label">${look ? '点自己的牌，随时看牌' : esc(p?.hand_name || (p?.folded ? '已弃牌' : p?.seen ? '已看牌' : t.game === 'zjh' ? '闷牌中' : '两张底牌 + 五张公共牌，选最好的五张'))}</div>` : `
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
  }
  function tick(c) {
    if (!live(c)) return;
    const t = c.table, seconds = Math.max(0, Math.ceil((t.turn_deadline || now(c)) - now(c)));
    $$('[data-gc-seconds]').forEach(el => el.textContent = seconds);
    $$('[data-gc-countdown]').forEach(el => el.textContent = Math.max(0, Math.ceil(t.countdown_at - now(c))));
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
    $$('[data-gc-history]', root).forEach(b => b.onclick = () => resultSheet(c, c.history[0]));
    $('[data-gc-verify]', root).onclick = () => verify(c, t.hand || t.last_hand);
    $('[data-gc-auto]', root).onclick = () => tableWrite(c, 'auto', { enabled: !me.auto });
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
    const t = c.table;
    showSheet(`
      <h3>${t.game === 'holdem' && t.phase === 'playing' ? '本手结算后离桌' : '确认离桌？'}</h3>
      <p>${t.game === 'zjh' ? '正在玩的这一手会按弃牌处理；已投入的积分不退，结算后退回剩余额度。' : '申请后轮到你时自动弃牌，不再投入；已经全下则保留分池资格。本手结算后退回剩余筹码。'}</p>
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
        <span class="tiny muted">不抽水 · ${holdem ? '牌桌仍会连续发牌' : '继续玩需重新准备'}</span>
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
      <button class="btn block" data-gc-result-verify>复核这手洗牌</button>
      <button class="link gc-close" data-cancel>回到牌桌</button>`, root => {
      $('[data-gc-result-verify]', root).onclick = () => verify(c, h);
      $$('[data-gc-hand]', root).forEach(b => b.onclick = () => resultSheet(c, c.history.find(x => x.number === Number(b.dataset.gcHand))));
      $$('[data-gc-focus]', root).forEach(b => b.onclick = () => resultSheet(c, h, b.dataset.gcFocus));
    });
  }
  async function verify(c, h) {
    if (!h) return;
    const generation = sheetGeneration;
    try {
      const v = await get(`/tables/${encodeURIComponent(c.id)}/hands/${h.number}/verify`);
      if (!live(c) || generation !== sheetGeneration) return;
      const observed = c.commitments?.get(h.number) || h.commitment;
      showSheet(`
        <h3>洗牌复核 · 第 ${v.hand_no} 手</h3>
        <div class="gc-note">发牌时先公布指纹，结算后公开牌序和盐值。在本页面重算 SHA-256，不把结果交给服务器代算。</div>
        <div class="tiny muted">开局承诺</div>
        <code class="gc-code">${esc(v.commitment)}</code>${v.settled ? `
          <div class="tiny muted">盐值</div>
          <code class="gc-code">${esc(v.salt)}</code>
          <div class="tiny muted">完整 52 张牌序</div>
          <code class="gc-code">${esc(v.deck_codes)}</code>
          <div class="gc-note">计算内容：UTF-8(牌序 + "|" + 盐值)，无空格。s/h/c/d 是黑桃/红桃/梅花/方块。
            ${c.table.game === 'zjh' ? '依发牌顺序每人连续取三张。' : `
              发牌从按钮左侧开始，按座位顺时针每人一张，共两圈。
              <br>顺序：${(v.deal_order || []).map(id => esc(c.table.seats.find(s => s.player === id)?.name || '已离桌玩家')).join(' → ')}。
              翻牌、转牌、河牌前各烧一张；提前结束不会补发后续公共牌。
              <br>N 人时，第 i 位底牌取牌序 i 和 N+i（从 0 起），公共牌取 2N+1、2N+2、2N+3、2N+5、2N+7。`}
            <br>这能检查牌序是否符合开局承诺，不等于对随机性的完整证明。公开整副牌序后，可推导已弃牌的牌。
          </div>
          <button class="btn block" data-gc-recheck>在本机重新计算</button>
          <div data-gc-verify-result role="status"></div>` : '<div class="gc-notice">本手尚未结束，牌序与盐值不会提前公开。</div>'}
        <button class="link gc-close" data-cancel>回到牌桌</button>`, root => {
        const b = $('[data-gc-recheck]', root);
        if (b) b.onclick = async () => {
          b.disabled = true;
          try {
            const bytes = new TextEncoder().encode(v.deck_codes + '|' + v.salt);
            const hash = crypto.subtle ? Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), x => x.toString(16).padStart(2, '0')).join('') : sha256(String.fromCharCode(...bytes));
            const codes = Array.isArray(v.deck) ? v.deck.map(x => '23456789TJQKA'[x % 13] + 'shcd'[Math.floor(x / 13)]).join(',') : '';
            const validDeck = v.deck?.length === 52 && new Set(v.deck).size === 52 && v.deck.every(x => Number.isInteger(x) && x >= 0 && x < 52) && codes === v.deck_codes;
            let dealMatches = true;
            if (c.table.game === 'holdem') {
              const order = v.deal_order || [], n = order.length;
              const board = [1, 2, 3, 5, 7].map(i => v.deck?.[2 * n + i]);
              // Compare only cards present in the viewer snapshot, never infer a hidden hole.
              dealMatches = n >= 2 && (h.engine.board || []).every((code, i) => code === board[i])
                && h.engine.players.every(p => !p.cards || p.cards.every((code, i) => {
                  const seat = order.indexOf(p.player);
                  return seat >= 0 && code === v.deck?.[seat + i * n];
                }));
            }
            const ok = validDeck && dealMatches && hash === v.commitment && (!observed || observed === v.commitment);
            $('[data-gc-verify-result]', root).innerHTML = `
              <p class="${ok ? 'gc-up' : 'gc-down'}"><b>${ok ? '✓ 与开局承诺一致，本手牌序未改变' : '✗ 复核不一致，请保存信息并联系管理员'}</b></p>
              ${ok && c.table.game === 'holdem' ? '<div class="small gc-up">发牌、烧牌顺序与本快照可见牌面一致</div>' : ''}
              <code class="gc-code">本机 SHA-256：${hash}</code>`;
          } catch (e) { $('[data-gc-verify-result]', root).textContent = '本机计算失败：' + e.message; }
          finally { b.disabled = false; }
        };
      });
    } catch (e) { if (live(c)) toast(e.message, true); }
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
  function rules(game = 'zjh', config = {}) {
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
          A23 是最小顺子，不比花色；最终摊牌完全一样就平分。235 吃豹子：${config.allow_235 ? '本桌开启' : '默认关闭，以牌桌设置为准'}。</p>
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
          <li><b>盲注：</b>每手庄家 D 轮转，小盲、大盲先自动放入一小笔筹码。两人桌庄家就是小盲，翻牌前先行动、之后后行动。</li>
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
        <p>开局先公布洗牌指纹，结束后点“复核”可在本机重算。普通牌桌不会把别人的未公开手牌发给你。</p>
      </div>
      <button class="btn block" data-cancel>知道了</button>`);
  }

  // SHA-256 fallback from the approved self-contained preview (input is UTF-8 bytes).
  function sha256(ascii) {
    const rr = (v, a) => (v >>> a) | (v << (32 - a)), maxWord = 2 ** 32;
    const words = [], bitLen = ascii.length * 8, k = [], composite = {};
    let hash = [];
    for (let cand = 2, n = 0; n < 64; cand++) {
      if (!composite[cand]) {
        for (let i = 0; i < 313; i += cand) composite[i] = cand;
        hash[n] = (cand ** 0.5 * maxWord) | 0;
        k[n++] = (cand ** (1 / 3) * maxWord) | 0;
      }
    }
    hash = hash.slice(0, 8); ascii += '\x80';
    while (ascii.length % 64 - 56) ascii += '\x00';
    for (let i = 0; i < ascii.length; i++) words[i >> 2] |= ascii.charCodeAt(i) << ((3 - i) % 4) * 8;
    words[words.length] = (bitLen / maxWord) | 0; words[words.length] = bitLen;
    for (let j = 0; j < words.length;) {
      const w = words.slice(j, (j += 16)), old = hash;
      hash = hash.slice(0, 8);
      for (let i = 0; i < 64; i++) {
        const w15 = w[i - 15], w2 = w[i - 2], a = hash[0], e = hash[4];
        const t1 = hash[7] + (rr(e, 6) ^ rr(e, 11) ^ rr(e, 25)) + ((e & hash[5]) ^ (~e & hash[6])) + k[i]
          + (w[i] = i < 16 ? w[i] : (w[i - 16] + (rr(w15, 7) ^ rr(w15, 18) ^ (w15 >>> 3)) + w[i - 7] + (rr(w2, 17) ^ rr(w2, 19) ^ (w2 >>> 10))) | 0);
        const t2 = (rr(a, 2) ^ rr(a, 13) ^ rr(a, 22)) + ((a & hash[1]) ^ (a & hash[2]) ^ (hash[1] & hash[2]));
        hash = [(t1 + t2) | 0].concat(hash); hash[4] = (hash[4] + t1) | 0;
      }
      for (let i = 0; i < 8; i++) hash[i] = (hash[i] + old[i]) | 0;
    }
    let out = '';
    for (let i = 0; i < 8; i++) for (let j = 3; j + 1; j--) out += ((hash[i] >> (j * 8)) & 255).toString(16).padStart(2, '0');
    return out;
  }
  return { lobby, table, activeTable, dispose };
};

