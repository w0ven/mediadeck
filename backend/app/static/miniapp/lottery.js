/* Real lottery screens inside the member Mini App navigation/authentication. */
'use strict';
window.createLotteryUI = ({ api, esc, setScreen, push, back, toast, stamp, tg, icon }) => {
  const root = '/api/app/lottery';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const image = (key, size = 32) => `<img class="emoji" width="${size}" height="${size}" src="/app/assets/lottery-assets/${key}.png" alt="">`;
  const lucky = () => `<div class="lucky" aria-label="LUCKY">${['L','U','C','K','Y'].map(k => image(k, 26)).join('')}</div>`;
  const titles = { draft: '草稿', published: '报名中', settled: '已开奖', cancelled: '已取消' };
  const states = {
    pending: '待处理', delivered: '已发放', binding: '待绑定领取', manual: '待人工发放',
    failed: '失败待恢复', blocked: '待管理员处理', unknown: '需核验', sent: '已发送',
    sending: '发送中', waiting: '待发送', idle: '发送中', off: '未开启', skipped: '已跳过',
    unreachable: '无法私信（未私聊过 Bot）'
  };
  const kinds = { shop: '商城道具 · 自动入背包', points: '积分 · 直接入余额', invite: '可用邀请名额', registration: '本人专属注册资格', manual: '自定义奖品 · 人工发放' };
  const audience = { all: '全部群友', member: '仅已注册', guest: '仅未注册' };
  const art = { shop: 'gem', points: 'starCute', invite: 'rocket', registration: 'gift', manual: 'giftPurple' };
  const call = (path, body = {}) => api(root + path, { method: 'POST', body });
  let detailLoad = 0;
  let activeTimer = null;
  let activeSheetResolve = null;

  function clearTimer() {
    if (activeTimer) {
      clearInterval(activeTimer);
      activeTimer = null;
    }
  }

  function closeSheet(value = false) {
    const dim = $('#lot-sheet');
    if (!dim) return;
    dim.classList.remove('active');
    setTimeout(() => {
      dim.remove();
      const done = activeSheetResolve;
      activeSheetResolve = null;
      if (done) done(value);
    }, 240);
  }

  function showSheet({ title, desc, html = false, iconName = 'giftPink', okText = '确认', cancelText = '取消', danger = false, onConfirm }) {
    if (activeSheetResolve) closeSheet(false);
    return new Promise(resolve => {
      activeSheetResolve = resolve;
      const wrap = document.createElement('div');
      wrap.id = 'lot-sheet';
      wrap.className = 'lot-sheet-dim';
      const descHtml = html ? desc : `<p class="lot-sheet-desc">${esc(desc)}</p>`;
      wrap.innerHTML = `<div class="lot-sheet-card" role="dialog" aria-modal="true">
        <div class="lot-sheet-grab"></div>
        <div class="lot-sheet-body">
          <img class="lot-sheet-icon" src="/app/assets/lottery-assets/${iconName}.png" alt="">
          <h3>${esc(title)}</h3>
          ${descHtml}
          <div class="lot-sheet-actions">
            <button class="btn secondary" id="lot-cancel">${esc(cancelText)}</button>
            <button class="btn${danger ? ' danger' : ''}" id="lot-ok">${esc(okText)}</button>
          </div>
        </div>
      </div>`;
      document.body.appendChild(wrap);
      requestAnimationFrame(() => wrap.classList.add('active'));

      wrap.onclick = e => {
        if (e.target === wrap) closeSheet(false);
      };
      const cancelBtn = $('#lot-cancel', wrap);
      if (cancelBtn) cancelBtn.onclick = () => closeSheet(false);
      const okBtn = $('#lot-ok', wrap);
      if (okBtn) {
        okBtn.onclick = async () => {
          okBtn.disabled = true;
          try {
            await onConfirm();
            closeSheet(true);
          } catch (err) {
            toast(err.message, true);
          } finally {
            if (okBtn && okBtn.isConnected) okBtn.disabled = false;
          }
        };
      }
    });
  }

  function page(html) {
    clearTimer();
    if (activeSheetResolve) closeSheet(false);
    setScreen(`<section class="lottery">${html}</section>`);
  }

  function browserId() {
    const KEY = 'md.ux.k';
    const LEGACY = 'mediadeck.lottery.browser.v1';
    const HEX = /^[0-9a-f]{64}$/;
    try {
      let value = localStorage.getItem(KEY);
      if (HEX.test(value || '')) return value;
      const legacy = localStorage.getItem(LEGACY);
      value = HEX.test(legacy || '') ? legacy : Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2,'0')).join('');
      localStorage.setItem(KEY, value);
      if (localStorage.getItem(KEY) !== value) return '';
      if (value === legacy) localStorage.removeItem(LEGACY);
      return value;
    } catch (_) { return ''; }
  }

  function bind(selector, action) {
    document.querySelectorAll(selector).forEach(button => {
      button.onclick = async () => {
        if (button.disabled) return;
        button.disabled = true;
        try { await action(button); } catch (e) { toast(e.message, true); }
        finally { if (button.isConnected) button.disabled = false; }
      };
    });
  }

  const openBot = (url) => { if (tg) tg.openTelegramLink(url); else window.open(url, '_blank', 'noopener'); };

  function head(title, side = '') {
    return `<div class="lot-head"><h2>${esc(title)}</h2>${side ? `<div class="lot-head-side">${side}</div>` : ''}</div>`;
  }

  function renderCountdownBlocks(sec) {
    if (sec <= 0) {
      return '<div class="cd-status-badge">报名已截止</div>';
    }
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const isUrgent = sec <= 300;
    const pad = n => String(n).padStart(2, '0');

    if (d > 0) {
      return `<div class="cd-blocks">
        <div class="cd-unit"><span class="cd-val num">${d}</span><span class="cd-lbl">天</span></div>
        <span class="cd-colon">:</span>
        <div class="cd-unit"><span class="cd-val num">${pad(h)}</span><span class="cd-lbl">时</span></div>
        <span class="cd-colon">:</span>
        <div class="cd-unit"><span class="cd-val num">${pad(m)}</span><span class="cd-lbl">分</span></div>
        <span class="cd-colon">:</span>
        <div class="cd-unit"><span class="cd-val num">${pad(s)}</span><span class="cd-lbl">秒</span></div>
      </div>`;
    }
    return `<div class="cd-blocks">
      <div class="cd-unit ${isUrgent ? 'urgent' : ''}"><span class="cd-val num">${pad(h)}</span><span class="cd-lbl">时</span></div>
      <span class="cd-colon">:</span>
      <div class="cd-unit ${isUrgent ? 'urgent' : ''}"><span class="cd-val num">${pad(m)}</span><span class="cd-lbl">分</span></div>
      <span class="cd-colon">:</span>
      <div class="cd-unit ${isUrgent ? 'urgent' : ''}"><span class="cd-val num">${pad(s)}</span><span class="cd-lbl">秒</span></div>
    </div>`;
  }

  function hero(c, state, count = 0) {
    const isSettled = state === 'settled';
    const isCancelled = state === 'cancelled';
    const statusText = isCancelled ? '已取消' : isSettled ? '已开奖' : '报名中';
    const statusPillClass = isCancelled ? 'red' : isSettled ? 'gold' : 'green';
    const drawModeDesc = c.draw_mode === 'count' ? '满员开奖' : c.draw_mode === 'timed' ? '定时开奖' : '手动开奖';
    const initialSec = c.deadline ? c.deadline - Math.floor(Date.now() / 1000) : 0;
    const isCount = c.draw_mode === 'count';

    let timingHtml = '';
    if (isCount) {
      timingHtml = `<span>开奖：</span><b>满 ${c.max_entries || 0} 人自动开奖 · 进度 ${count || 0}/${c.max_entries || 0} 人</b>`;
    } else if (c.deadline) {
      timingHtml = `<span>截止：</span><b>${stamp(c.deadline, true)}</b>`;
    } else {
      timingHtml = `<span>截止：</span><b>${c.draw_mode === 'manual' ? '开奖前均可报名' : '待设置'}</b>`;
    }

    return `<div class="lot-hero">
      <div class="hero-sheen"></div>
      <div class="hero-top-row">
        ${lucky()}
        <span class="pill ${statusPillClass}">${statusText}</span>
      </div>
      <h3>${esc(c.title)}</h3>
      ${!isSettled && !isCancelled && c.deadline ? `<div class="hero-countdown-box" id="lot-countdown-box">${renderCountdownBlocks(initialSec)}</div>` : ''}
      <img class="hero-art" src="/app/assets/lottery-assets/giftPink.png" alt="礼物">
      <div class="hero-bottom">
        <div>${timingHtml}</div>
        <span class="hero-draw-mode">${drawModeDesc}</span>
      </div>
    </div>`;
  }

  function description(p) {
    if (p.kind === 'registration') return `${esc(p.group_name || '')} · ${p.days} 天 · 仅未注册本人`;
    if (p.kind === 'points') return `每人 ${p.amount} 积分 · 直接到账`;
    if (p.kind === 'invite') return `每人 ${p.amount} 个可用名额`;
    if (p.kind === 'manual') return '中奖后查看领取说明';
    const t = p.item_terms || p.spec;
    if (!t) return '规格已锁定 · 入背包生效';
    if (t.kind === 'custom') return `私密说明入背包 · 保留 ${t.retention_days} 天`;
    const term = t.duration_days ? `${t.duration_days} 天` : '长期有效';
    const effect = { bandwidth_card: `+${t.amount} Mbps`, streams_card: `+${t.amount} 路`, invite_card: '1 个可用邀请名额', title_card: '创建个人称号', whitelist_card: '白名单资格' }[t.kind] || '商城道具';
    return `${effect} · ${term}`;
  }

  function prizeTag(p, isV2 = false) {
    if (!isV2) return '';
    if (p.kind === 'registration') return '<span class="prize-tag tag-guest">仅未注册</span>';
    if (p.kind === 'manual') return '<span class="prize-tag tag-all">通用奖品</span>';
    return '<span class="prize-tag tag-member">仅已注册</span>';
  }

  function prizes(c) {
    const isV2 = c.eligibility_version === 2;
    return `<div class="prize-list">${c.prizes.map(p => `<div class="prize-row"><div class="prize-icon">${image(art[p.kind], 34)}</div><div class="grow"><div class="prize-name">${esc(p.name)}${prizeTag(p, isV2)}</div><div class="prize-desc">${description(p)}</div></div><div class="prize-count num">${p.quantity} 名</div></div>`).join('')}</div>`;
  }

  function awardCard(a) {
    if (!a) return '';
    const badgeText = esc(states[a.state] || a.state);
    return `<div class="result-banner winner-card">
      <div class="row">
        ${image(art[a.prize.kind], 40)}
        <div class="grow">
          <h3>恭喜中奖！好运降临</h3>
          <div class="award-title-row">
            <b>${esc(a.prize.name)}</b>
            <span class="award-badge">${badgeText}</span>
          </div>
        </div>
      </div>
      <p>${a.consumed ? '资格已使用' : ''}${a.error ? '\n' + esc(a.error) : ''}${a.instructions ? '\n' + esc(a.instructions) : ''}</p>
      ${a.state === 'binding' ? '<p class="small">请在 Bot 绑定或注册账号后领取。</p>' : ''}
      ${a.prize.kind === 'shop' && a.state === 'delivered' ? '<p class="small">道具已入背包，使用后生效。</p>' : ''}
      ${a.registration_url ? '<button class="btn" id="lot-register">进入 Bot 注册 · 本人专属</button>' : ''}
      ${['binding','pending','failed'].includes(a.state) ? '<button class="btn" id="lot-claim">刷新领取奖励</button>' : ''}
    </div>`;
  }

  async function detail(lid, alive = () => true, mode = 'participant') {
    const load = ++detailLoad;
    const [ctx, d] = await Promise.all([api(root + '/context'), api(root + '/' + lid)]);
    if (!alive() || load !== detailLoad) return;
    const c = d.config, admin = ctx.is_admin;
    const isManage = admin && (mode === 'manage' || d.state === 'draft');
    const closed = d.state !== 'published' || Boolean(c.deadline && Date.now() >= c.deadline * 1000);
    const eligible = c.audience === 'all' || (c.audience === 'member') === ctx.registered;
    const refresh = () => detail(lid, alive, isManage ? 'manage' : 'participant');
    const total = c.prizes.reduce((s, p) => s + p.quantity, 0);
    const fee = c.entry_fee || 0;
    const balance = ctx.points_balance;
    const hasEnough = !fee || (balance != null && balance >= fee);
    const hasRegPrize = c.prizes.some(p => p.kind === 'registration');

    // Header buttons (Pill button with Lucide rotate-cw icon for refresh)
    const sideBtns = [];
    if (admin) {
      if (isManage) {
        sideBtns.push(`<button class="lot-top-btn" id="lot-to-participant">${icon ? icon('ticket') : ''}参与模式</button>`);
      } else {
        sideBtns.push(`<button class="lot-top-btn" id="lot-to-manage">${icon ? icon('wrench') : ''}管理本期</button>`);
      }
    }
    sideBtns.push(`<button class="lot-top-btn" id="lot-refresh">${icon ? icon('rotate-cw') : ''}刷新</button>`);

    // Setup live countdown for active lottery
    function startCountdown() {
      clearTimer();
      if (d.state === 'settled' || !c.deadline) return;
      activeTimer = setInterval(() => {
        if (!alive() || load !== detailLoad) {
          clearTimer();
          return;
        }
        const left = c.deadline - Math.floor(Date.now() / 1000);
        const el = $('#lot-countdown-box');
        if (el) {
          el.innerHTML = renderCountdownBlocks(left);
        }
        if (left <= 0) {
          clearTimer();
        }
      }, 1000);
    }

    if (isManage) {
      // Admin Management Mode
      const adminRulesText = d.admin_rules ? `${d.rules}\n\n【防小号配置】\n${d.admin_rules}` : d.rules;
      // All published activities (timed, count, manual) offer explicit draw button for administrators
      const drawButton = d.state === 'published' ? '<button class="btn" id="lot-draw">立即开奖</button>' : '';
      const statsHtml = d.state === 'settled'
        ? `<div class="lot-stats cols-3">
            <div class="stat"><b class="num">${d.count}</b><span>参与人数</span></div>
            <div class="stat"><b class="num">${total}</b><span>奖品名额</span></div>
            <div class="stat"><b class="num">${d.winners.length}</b><span>已中奖</span></div>
          </div>`
        : `<div class="lot-stats cols-2">
            <div class="stat"><b class="num">${d.count}</b><span>参与人数</span></div>
            <div class="stat"><b class="num">${total}</b><span>奖品名额</span></div>
          </div>`;

      page(`${head('抽奖中心 · 管理', sideBtns.join(''))}
        <div class="lot-role-bar">
          <span class="pill">${image('shield', 15)} 管理模式</span>
        </div>
        ${hero(c, d.state, d.count)}
        ${statsHtml}
        <div class="notice">
          <div class="row between">
            <span>费用：${fee ? fee + ' 积分' : '免费'} · ${esc(audience[c.audience])}</span>
            <div style="display:flex;gap:6px;align-items:center">
              <span class="pill">${c.anti_multi ? '严格防小号' : '普通防刷'}</span>
              ${c.anti_multi && c.device_check ? '<span class="pill">设备校验</span>' : ''}
            </div>
          </div>
          <div style="margin-top:10px;display:flex;gap:8px">
            <button class="btn secondary" style="flex:1" id="lot-records">报名记录与关联异常</button>
            <button class="btn secondary" style="flex:none" id="lot-copy">复制本期</button>
          </div>
        </div>
        <h3>本期奖池 <span class="section-tag">${esc(audience[c.audience])}可参与</span></h3>${prizes(c)}
        ${adminStatus(d)}
        <details class="lot-fold">
          <summary><span>活动规则</span><span class="fold-arrow">${icon ? icon('chevron-down') : '▼'}</span></summary>
          <div class="rule-note">${image('shield', 20)}<span>${esc(adminRulesText)}</span></div>
        </details>
        <div class="lot-actions">
          ${d.state === 'draft' ? '<button class="btn danger-outline" id="lot-delete">删除草稿</button><button class="btn secondary" id="lot-edit">编辑草稿</button><button class="btn" id="lot-preview">发布前预览</button>' : d.state === 'published' ? '<button class="btn danger-outline" id="lot-cancel-act">取消活动</button><button class="btn secondary" id="lot-list">活动列表</button>' + drawButton + '' : '<button class="btn secondary" id="lot-list">活动列表</button><button class="btn" id="lot-retry-awards">重试未发奖励</button>'}
        </div>
        <div class="lot-bottom-spacer"></div>`);

      startCountdown();
      bind('#lot-to-participant', () => back());
      bind('#lot-refresh', refresh);
      bind('#lot-list', () => push(list));
      bind('#lot-copy', async () => {
        const copyDraft = await call(`/${lid}/copy`);
        toast('已复制为新草稿');
        push(a => editor(a, copyDraft, true));
      });
      bind('#lot-delete', () => showSheet({
        title: '确认删除草稿？',
        desc: `${c.title}\n删除后草稿将永久移除，不可恢复。`,
        okText: '确认删除',
        danger: true,
        onConfirm: async () => {
          await call(`/${lid}/delete`, { confirm: true });
          toast('草稿已删除');
          back();
        }
      }));
      bind('#lot-cancel-act', () => showSheet({
        title: '确认取消本期活动？',
        desc: '取消后不再报名和开奖；收费活动将全额退还报名积分，并在群里发取消通知。',
        okText: '确认取消活动',
        danger: true,
        onConfirm: async () => {
          await call(`/${lid}/cancel`, { confirm: true });
          toast('活动已取消');
          await refresh();
        }
      }));
      bind('#lot-edit', () => push(a => editor(a, d)));
      bind('#lot-preview', () => push(a => preview(lid, a)));
      bind('#lot-records', () => push(a => records(lid, a)));
      bind('#lot-draw', () => showSheet({
        title: '确认立即开奖？',
        desc: `${c.title}\n${d.count} 人报名 · ${total} 个名额\n立即停止报名并开奖，开奖后结果永久锁定。`,
        okText: '确认立即开奖',
        onConfirm: async () => { await call(`/${lid}/draw`); await refresh(); }
      }));
      bind('#lot-retry-awards', async () => { await call(`/${lid}/retry-awards`); await refresh(); });
      bind('[data-lot-confirm]', b => showSheet({
        title: '确认人工奖品已交付',
        desc: '请先实际完成交付。确认后将记录发放状态。',
        okText: '确认交付',
        onConfirm: async () => { await call(`/${lid}/awards/${b.dataset.lotConfirm}/confirm`); await refresh(); }
      }));
      bind('[data-lot-recover]', b => push(a => recover(lid, d.notices.find(n => String(n.id) === b.dataset.lotRecover), a)));
      return;
    }

    // Participant Mode (Default for all users, including Admin from group deep link)
    const isCancelled = d.state === 'cancelled';
    const isSettled = d.state === 'settled';

    // Participation thresholds
    const minL = c.min_level || 0;
    const minP = c.min_points || 0;
    const minW = c.min_watch_hours || 0;
    const hasThreshold = minL > 0 || minP > 0 || minW > 0;
    const thresholdLabels = [];
    if (minL > 0) thresholdLabels.push(`Lv${minL} 及以上`);
    if (minP > 0) thresholdLabels.push(`积分余额 ≥ ${minP}`);
    if (minW > 0) thresholdLabels.push(`近 30 天观看 ≥ ${minW} 小时`);

    const pointsShort = (minP > 0 && balance != null && balance < minP) ? (minP - balance) : 0;
    const thresholdBlocked = !ctx.registered && hasThreshold;
    const prizeEligible = d.prize_eligible !== false;
    const prizeIneligibleReason = d.prize_eligibility_reason || '当前无可抽奖项';
    const isOpen = d.state === 'published' && !closed && !isCancelled;
    const needPrivateStart = Boolean(!d.joined && isOpen && d.bot_private_required && !d.bot_private_ready);
    const privateStartReason = d.bot_private_reason || '报名须先私聊启动 Bot';

    let joinText = d.joined ? '已报名 · 等待开奖'
      : isCancelled ? '活动已取消'
      : closed ? '报名已截止'
      : needPrivateStart ? '须先私聊启动 Bot'
      : thresholdBlocked ? '须绑定已注册账户'
      : pointsShort > 0 ? `积分不足（差 ${pointsShort} 积分）`
      : !eligible ? '当前身份不可参与'
      : !prizeEligible ? prizeIneligibleReason
      : fee > 0 && balance == null ? '须绑定已注册账户'
      : fee > 0 && !hasEnough ? `积分不足（需 ${fee} 积分）`
      : fee > 0 ? `支付 ${fee} 积分报名`
      : '立即参与';

    const canJoin = !isCancelled && !closed && !d.joined && !needPrivateStart && eligible && prizeEligible && hasEnough && !thresholdBlocked && pointsShort === 0 && (fee === 0 || balance != null);

    const statsHtml = isSettled
      ? `<div class="lot-stats cols-3">
          <div class="stat"><b class="num">${d.count}</b><span>参与人数</span></div>
          <div class="stat"><b class="num">${total}</b><span>奖品名额</span></div>
          <div class="stat"><b class="num">${d.winners.length}</b><span>已中奖</span></div>
        </div>`
      : `<div class="lot-stats cols-2">
          <div class="stat"><b class="num">${d.count}</b><span>参与人数</span></div>
          <div class="stat"><b class="num">${total}</b><span>奖品名额</span></div>
        </div>`;

    page(`${head('重生之我有面板', sideBtns.join(''))}
      <div class="lot-role-bar">
        <span class="pill">${image(admin ? 'shield' : ctx.registered ? 'verified' : 'starCute', 15)} ${admin ? '管理员参与' : ctx.registered ? '已注册会员' : '未注册群友'}</span>
      </div>
      ${hero(c, d.state, d.count)}
      ${statsHtml}
      ${isCancelled ? `<div class="result-banner consolation-card"><h3>本期活动已取消</h3><p class="small">${d.joined && fee > 0 ? '报名积分已全额退还。' : '本期不再报名和开奖。'}</p></div>` : ''}
      ${fee > 0 && !isCancelled ? `<div class="ticket"><div><strong>报名费用：${fee} 积分</strong><small>我的余额：${balance != null ? balance + ' 积分' : '未注册'} · 成功报名不退（未中奖不退）</small></div></div>` : ''}
      ${d.my_award ? awardCard(d.my_award) : ''}
      ${isSettled && !d.my_award ? `<div class="result-banner consolation-card"><h3>${d.joined ? '这次与好运擦肩而过' : '本期抽奖已结束'}</h3><p class="small">感谢参与，期待下期好运！</p></div>` : ''}
      ${d.joined && !isSettled && !isCancelled ? `<div class="ticket">${image('verified', 26)}<div><strong>报名已记录</strong><small>每人每期仅一次 · 等待开奖</small></div></div>` : ''}
      ${!ctx.registered && fee === 0 && !isCancelled && !hasThreshold ? '<div class="rule-note">未注册群友可免费报名。中奖后按提示在 Bot 绑定或注册即可领取；本人专属注册资格绑定中奖者本人，不可转赠。</div>' : ''}
      ${ctx.registered && hasRegPrize && !isCancelled ? '<div class="rule-note">已注册会员可报名本期其他奖项；本人专属注册资格仅在未注册者中抽取。</div>' : ''}
      <h3>本期奖池 <span class="section-tag">${esc(audience[c.audience])}可参与</span></h3>${prizes(c)}
      ${isSettled ? `<h3>本期幸运名单 · ${d.winners.length} 位</h3><div class="prize-list">${d.winners.map((w, i) => `<div class="winner"><span class="muted tiny num">${i + 1}</span><div class="avatar">${esc(w.label.slice(0, 1))}</div><div class="grow"><b>${esc(w.label)}</b><div class="prize-desc">${esc(w.prize.name)}</div></div>${image('party', 22)}</div>`).join('') || '<div class="empty">本期未产生中奖者</div>'}</div>` : ''}
      ${needPrivateStart ? `<div class="rule-note banner bad" id="lot-private-banner">${image('notice', 18)}<span>${esc(privateStartReason)}</span></div>` : ''}
      <details class="lot-fold">
        <summary><span>查看活动规则与说明</span><span class="fold-arrow">${icon ? icon('chevron-down') : '▼'}</span></summary>
        <div class="rule-note">${image('star', 18)}<span>${esc(d.rules)}</span></div>
      </details>
      ${!isCancelled ? `<div class="lot-actions">
        ${thresholdLabels.length ? `<div class="lot-threshold-pills">${thresholdLabels.map(t => `<span class="pill">${image('starCute', 13)} ${esc(t)}</span>`).join('')}</div>` : ''}
        ${needPrivateStart
          ? `<button class="btn" id="lot-start-bot">${image('megaphone', 16)} 先私聊启动 Bot</button><button class="btn secondary" id="lot-retry-private">我已启动，刷新报名</button>`
          : (!isSettled ? `<button class="btn" id="lot-join" ${canJoin ? '' : 'disabled'}>${joinText}</button>` : '<button class="btn secondary" id="lot-result-refresh">刷新领奖状态</button>')}
      </div>` : ''}
      <div class="lot-bottom-spacer"></div>`);

    startCountdown();
    if (admin) bind('#lot-to-manage', () => push(a => detail(lid, a, 'manage')));
    bind('#lot-refresh,#lot-result-refresh,#lot-retry-private', refresh);
    bind('#lot-start-bot', () => {
      if (d.bot_start_url) openBot(d.bot_start_url);
      else toast('未获取到 Bot 私聊链接，请稍后刷新');
    });
    bind('#lot-claim', async () => { await call(`/${lid}/claim`); await refresh(); });
    bind('#lot-register', () => openBot(d.my_award.registration_url));
    bind('#lot-join', async () => {
      async function doJoin(devicePayload) {
        const body = { browser_id: browserId(), expected_entry_fee: fee };
        if (devicePayload) {
          body.consent_version = devicePayload.consent_version;
          body.ctx = devicePayload.ctx;
        }
        await call(`/${lid}/join`, body);
        toast('报名已记录');
        await refresh();
      }

      async function collectDevice() {
        if (!window.__mdx || typeof window.__mdx.collect !== 'function') {
          throw new Error('未能完成设备校验，请重试');
        }
        try {
          return await window.__mdx.collect();
        } catch (_) {
          throw new Error('未能完成设备校验，请重试');
        }
      }

      const consent = d.consent;
      if (consent) {
        if (fee > 0) {
          // 合并弹层：设备校验说明 + 扣费信息
          showSheet({
            title: consent.title || '设备校验与扣费报名',
            desc: `<p class="lot-sheet-desc">${esc(consent.body || '')}</p><div class="lot-sheet-ticket"><strong>报名费用：${fee} 积分</strong><small>我的余额：${balance} 积分 · 成功报名后不退（未中奖也不退）</small></div>`,
            html: true,
            okText: `同意并支付 ${fee} 积分报名`,
            cancelText: consent.decline || '不同意',
            onConfirm: async () => {
              const ctxBlob = await collectDevice();
              await doJoin({ consent_version: consent.version, ctx: ctxBlob });
            }
          });
        } else {
          // 仅设备校验弹层
          showSheet({
            title: consent.title || '设备校验',
            desc: consent.body || '',
            okText: consent.accept || '同意并报名',
            cancelText: consent.decline || '不同意',
            onConfirm: async () => {
              const ctxBlob = await collectDevice();
              await doJoin({ consent_version: consent.version, ctx: ctxBlob });
            }
          });
        }
      } else {
        // 无设备校验活动
        if (fee > 0) {
          showSheet({
            title: '确认扣费报名',
            desc: `将扣除 ${fee} 积分（余额 ${balance} 积分）\n成功报名后不退（未中奖也不退）`,
            okText: `支付 ${fee} 积分并报名`,
            onConfirm: async () => {
              await doJoin();
            }
          });
        } else {
          await doJoin();
        }
      }
    });
  }

  function adminStatus(d) {
    const noticeKinds = {
      launch: '群发起公告',
      result: '群开奖公示',
      reminder: '群截止前提醒',
      cancel: '群取消通知'
    };
    const opKinds = {
      pin: '公告置顶',
      unpin: '取消置顶',
      live: '群公告报名人数更新',
      reminder: '截止前 1 小时群提醒'
    };
    const noticesHtml = (d.notices || []).map(n => {
      const err = n.error ? esc(n.error) : '';
      const msg = n.message_id ? `消息 #${n.message_id}` : '';
      const meta = [err, msg].filter(Boolean).join(' · ');
      return `<div class="notice">
        <div class="row between"><b>${noticeKinds[n.kind] || '群公告'}</b><span class="pill">${esc(states[n.state] || n.state)}</span></div>
        ${meta ? `<p class="tiny muted">${meta}</p>` : ''}
        ${['failed','unknown'].includes(n.state) ? `<button class="btn secondary" data-lot-recover="${n.id}">核验 / 恢复公告</button>` : ''}
      </div>`;
    }).join('');

    // Operations status (pin, reminder, live_count, winner_dm)
    let opsHtml = '';
    const ops = d.operations || {};
    const opItems = [];
    if (ops.pin && ops.pin.state !== 'off') opItems.push({ kind: 'pin', name: '发起公告静默置顶', ...ops.pin });
    if (ops.reminder && ops.reminder.state !== 'off') opItems.push({ kind: 'reminder', name: '截止前 1 小时提醒', ...ops.reminder });
    if (ops.live && ops.live.state !== 'off') {
      const shownDesc = ops.live.shown ? `当前显示：${esc(ops.live.shown)}` : '';
      opItems.push({ kind: 'live', name: '公告报名人数更新', ...ops.live, extra: shownDesc });
    }
    if (ops.winner_dm && ops.winner_dm.enabled) {
      const sum = ops.winner_dm.summary || {};
      const sumDesc = Object.entries(sum).map(([st, cnt]) => `${states[st] || st} ${cnt}`).join('，') || '暂无私信';
      opItems.push({ kind: 'winner_dm', name: '中奖私信通知', state: 'sent', extra: sumDesc });
    }
    if (opItems.length) {
      opsHtml = opItems.map(op => {
        const err = op.error ? esc(op.error) : '';
        const meta = [err, op.extra || ''].filter(Boolean).join(' · ');
        return `<div class="notice">
          <div class="row between"><b>${esc(op.name)}</b><span class="pill">${esc(states[op.state] || op.state)}</span></div>
          ${meta ? `<p class="tiny muted">${meta}</p>` : ''}
        </div>`;
      }).join('');
    }

    // Cancellation refunds summary
    let refundHtml = '';
    if (d.refunds) {
      refundHtml = `<div class="notice">
        <div class="row between"><b>活动取消退款</b><span class="pill green">已全额退还</span></div>
        <p class="tiny muted">已退还 ${d.refunds.count} 人，共计 ${d.refunds.points} 积分</p>
      </div>`;
    }

    // Awards status with DM
    const awardsHtml = (d.awards || []).map(a => {
      const dm = a.dm;
      const dmBadge = dm ? `<span class="pill tiny-pill ${dm.state === 'unreachable' ? 'red' : ''}">${dm.state === 'unreachable' ? '无法私信（未私聊过 Bot）' : '私信：' + (states[dm.state] || dm.state)}</span>` : '';
      return `<div class="notice">
        <div class="row between"><b>${esc(a.label)}</b><div style="display:flex;gap:4px;align-items:center">${dmBadge}<span class="pill">${esc(states[a.state] || a.state)}</span></div></div>
        <p class="small">${esc(a.prize.name)}</p>
        ${a.instructions ? `<p class="small">私密说明：${esc(a.instructions)}</p>` : ''}
        ${a.error ? `<p class="error">${esc(a.error)}</p>` : ''}
        ${a.prize.kind === 'manual' && a.state !== 'delivered' ? `<button class="btn secondary" data-lot-confirm="${a.id}">确认已人工发放</button>` : ''}
      </div>`;
    }).join('');

    return `<h3>管理与发放状态</h3>
      ${d.last_error ? `<p class="error">${esc(d.last_error)}</p>` : ''}
      ${refundHtml}
      ${noticesHtml}
      ${opsHtml}
      ${awardsHtml}`;
  }

  async function records(lid, alive = () => true, offset = 0) {
    const d = await api(`${root}/${lid}/records?offset=${offset}`);
    if (!alive()) return;
    const outcomes = { excluded: '异常排除 · 不参与中奖', winner: '已中奖', not_selected: '未抽中', eligible: '待开奖 · 资格核验中' };
    const defaultReasonMap = { ip: '相同可信公网 IP', device: '相同浏览器安装标识', hardware: '设备环境特征相同' };
    const linkReason = l => l.label || defaultReasonMap[l.reason] || l.reason;
    const clusterLabels = c => (c.labels && c.labels.length) ? c.labels.join(' / ') : (c.reasons || []).map(r => defaultReasonMap[r] || r).join(' / ');

    page(`${head('报名与异常记录', '<button class="lot-top-btn" id="lot-record-refresh">' + (icon ? icon('rotate-cw') : '') + '刷新</button>')}
      <div class="lot-stats cols-3">
        <div class="stat"><b class="num">${d.total}</b><span>报名记录</span></div>
        <div class="stat"><b class="num">${d.excluded}</b><span>异常排除</span></div>
        <div class="stat"><b class="num">${d.clusters.length}</b><span>关联簇</span></div>
      </div>
      <h3>关联异常账号</h3>
      ${d.clusters.map((c,i) => `<section class="notice lot-cluster"><div class="row between"><b>关联簇 ${i+1} · ${c.accounts.length} 个账号</b><span class="pill gold">全部排除</span></div><p class="small">${esc(clusterLabels(c))}</p>${c.accounts.map(a => `<div class="winner"><div class="avatar">${esc(a.label.slice(0,1))}</div><div class="grow"><b>${esc(a.label)}</b><div class="tiny muted">TG ${esc(a.tg_id)} · ${a.excluded ? '本期不参与中奖' : '需刷新核验'}</div></div></div>`).join('')}<details class="lot-fold"><summary class="text-btn"><span>查看关联证据</span><span class="fold-arrow">${icon ? icon('chevron-down') : '▼'}</span></summary>${c.links.map(e => `<div class="rule-note">${esc(linkReason(e))} · TG ${e.tg_ids.map(esc).join('、')}<br>${stamp(e.detected_at,true)} 检测到关联</div>`).join('')}</details></section>`).join('') || '<div class="empty">本期无关联异常账号</div>'}
      <h3>全部报名记录 · ${esc(titles[d.state])}</h3>
      <div class="prize-list">${d.entries.map(e => `<div class="prize-row"><div class="grow"><div class="prize-name">${esc(e.label)} <span class="tiny muted num">TG ${esc(e.tg_id)}</span></div><div class="prize-desc">${stamp(e.joined_at,true)} 报名 · ${e.registered ? '当时已注册' : '当时未注册'}</div><div class="small ${e.excluded ? 'error' : 'muted'}">${outcomes[e.outcome]}${e.award_state ? ' · ' + esc(states[e.award_state] || e.award_state) : ''}</div></div></div>`).join('') || '<div class="empty">暂无报名</div>'}</div>
      <div class="lot-actions">
        <button class="btn secondary" id="lot-record-back">返回本期活动</button>
        ${offset ? '<button class="btn secondary" id="lot-record-prev">上一页</button>' : ''}
        ${d.more ? '<button class="btn secondary" id="lot-record-next">下一页</button>' : ''}
      </div>
      <div class="lot-bottom-spacer"></div>`);

    bind('#lot-record-back', back);
    bind('#lot-record-refresh', () => records(lid, alive, offset));
    bind('#lot-record-prev', () => records(lid, alive, Math.max(0, offset - 50)));
    bind('#lot-record-next', () => records(lid, alive, offset + 50));
  }

  async function list(alive = () => true, offset = 0) {
    const data = await api(root + '/admin/list?offset=' + offset);
    if (!alive()) return;
    const gifts = ['giftPink', 'giftBlue', 'giftPurple'];
    page(`${head('抽奖中心', '<button class="btn" style="min-height:36px;padding:6px 14px;font-size:12.5px" id="lot-new">＋ 新建抽奖</button>')}
      ${data.items.map((d, i) => {
        const desc = d.draw_mode === 'count'
          ? `满 ${d.max_entries || 0} 人自动开奖 · 进度 ${d.count || 0}/${d.max_entries || 0} 人`
          : d.deadline ? stamp(d.deadline, true) + ' 截止 · 北京' : '未设截止 · 管理员开奖';
        return `<button class="activity-item" data-lot-open="${esc(d.id)}"><img class="activity-icon" src="/app/assets/lottery-assets/${gifts[i % gifts.length]}.png" alt=""><div class="grow"><b>${esc(d.title)}</b><div class="prize-desc">${desc}</div></div><span class="pill ${d.state === 'settled' ? 'gold' : 'green'}">${esc(titles[d.state])}</span></button>`;
      }).join('') || `<div class="empty"><img class="activity-icon" style="width:56px;height:56px;margin:0 auto 8px" src="/app/assets/lottery-assets/giftPink.png" alt=""><p>暂无活动，点击上方新建。</p></div>`}
      <div class="lot-actions">
        ${offset ? '<button class="btn secondary" id="lot-prev">上一页</button>' : ''}
        ${data.more ? '<button class="btn secondary" id="lot-next">下一页</button>' : ''}
      </div>
      <div class="lot-bottom-spacer"></div>`);

    bind('#lot-new', () => push(editor));
    bind('[data-lot-open]', b => push(a => detail(b.dataset.lotOpen, a, 'manage')));
    bind('#lot-prev', () => list(alive, Math.max(0, offset - 50)));
    bind('#lot-next', () => list(alive, offset + 50));
  }

  async function preview(lid, alive, fromManage = true) {
    const [d, p] = await Promise.all([api(root + '/' + lid), call(`/${lid}/preview`)]);
    if (!alive()) return;
    const copy = new DOMParser().parseFromString(p.text, 'text/html').body.textContent;
    page(`${head('发布前预览')}
      <p class="muted small">${image('megaphone', 20)} 以下正文将发往 ${esc(d.config.chat_id)}${d.config.thread_id ? ' · 话题 ' + d.config.thread_id : ''}</p>
      <article class="message">${lucky()}<br>${esc(copy)}</article>
      <div class="rule-note">发布后活动规则、奖项与截止时间全部锁定。</div>
      <div class="lot-actions">
        <button class="btn secondary" id="lot-cancel">返回修改</button>
        <button class="btn" id="lot-publish">确认发布到群</button>
      </div>
      <div class="lot-bottom-spacer"></div>`);

    bind('#lot-cancel', back);
    bind('#lot-publish', async () => {
      await call(`/${lid}/publish`, { revision: d.revision });
      toast('发布已处理');
      if (fromManage) {
        push(a => detail(lid, a, 'manage'));
      } else {
        back();
      }
    });
  }

  function recover(lid, n, alive) {
    if (!alive()) return;
    page(`${head('核验群公告')}
      <div class="rule-note">${esc(n.error)}<br>超时可能已发送成功，请先查看目标群。若已存在，请填写该消息 ID，系统将编辑原消息。</div>
      <div class="lot-field"><label for="lot-mid">已有公告消息 ID</label><input id="lot-mid" type="number" min="1" value="${n.message_id || ''}"></div>
      <label class="lot-check" for="lot-absent"><input id="lot-absent" type="checkbox"><span>我已人工核实目标群中不存在该公告</span></label>
      <div class="lot-actions lot-form-actions">
        <button class="btn secondary" id="lot-cancel">返回</button>
        <button class="btn" id="lot-recover-ok">确认恢复</button>
      </div>
      <div class="lot-bottom-spacer"></div>`);

    bind('#lot-cancel', back);
    bind('#lot-recover-ok', async () => {
      await call(`/${lid}/notices/${n.id}/recover`, { message_id: $('#lot-mid').value ? Number($('#lot-mid').value) : null, confirmed_absent: $('#lot-absent').checked });
      back();
    });
  }

  async function editor(alive = () => true, existing = null, isCopied = false) {
    const opts = await api(root + '/admin/options');
    if (!alive()) return;
    const isLegacyDeviceChecked = Boolean(existing && existing.config && existing.config.device_check);
    const draft = structuredClone(existing ? existing.config : {
      title: '', chat_id: opts.chats[0] || '', thread_id: null,
      audience: 'all', draw_mode: 'timed', max_entries: 100, deadline: null,
      prizes: [], anti_multi: true, device_check: false, entry_fee: 0,
      min_level: 0, min_points: 0, min_watch_hours: 0,
      reminder: true, pin: true
    });
    // Defaults for new draft or missing keys in legacy existing config
    draft.min_level = draft.min_level || 0;
    draft.min_points = draft.min_points || 0;
    draft.min_watch_hours = draft.min_watch_hours || 0;
    if (draft.max_entries === undefined || draft.max_entries === null) draft.max_entries = 100;
    if (draft.reminder === undefined) draft.reminder = true;
    if (draft.pin === undefined) draft.pin = true;
    if (draft.anti_multi === undefined) draft.anti_multi = true;
    draft.device_check = isLegacyDeviceChecked;

    function renderEditor() {
      const isManual = draft.draw_mode === 'manual';
      const isCount = draft.draw_mode === 'count';
      const isTimed = draft.draw_mode === 'timed';
      const isMemberOnly = draft.audience === 'member';
      const date = draft.deadline ? new Date((draft.deadline + 8 * 3600) * 1000).toISOString().slice(0, 16) : '';
      const options = (rows, value) => rows.map(([id, name]) => `<option value="${esc(id)}" ${String(id) === String(value) ? 'selected' : ''}>${esc(name)}</option>`).join('');
      const levelOpts = [['0', '不限'], ['1', 'Lv1'], ['2', 'Lv2'], ['3', 'Lv3'], ['4', 'Lv4'], ['5', 'Lv5'], ['6', 'Lv6'], ['7', 'Lv7'], ['8', 'Lv8']];
      const copiedNotice = isCopied && !draft.deadline && isTimed
        ? '<div class="banner bad" id="lot-copied-banner"><span>⚠️ 请重新设置截止时间（复制出的草稿截止时间已清空）</span></div>'
        : '';

      page(`${head(existing ? '编辑抽奖草稿' : '新建抽奖活动')}
        ${copiedNotice}
        <form id="lot-form" class="lot-form" novalidate>
          <div class="lot-field"><label for="lot-title">活动名称</label><input id="lot-title" required maxlength="60" value="${esc(draft.title)}" placeholder="请输入抽奖活动名称"></div>
          <div class="lot-field"><label for="lot-chat">发布白名单群</label><select id="lot-chat" required>${options(opts.chats.map(c => [c, c]), draft.chat_id)}</select></div>
          <div class="lot-field"><label for="lot-thread">话题 ID（仅话题群，可留空）</label><input id="lot-thread" type="number" min="1" value="${draft.thread_id || ''}"></div>
          <div class="lot-field"><label for="lot-audience">参与对象</label><select id="lot-audience">${options(Object.entries(audience), draft.audience)}</select></div>
          ${isMemberOnly ? `
          <div class="lot-field"><label for="lot-fee">报名费用（积分，0 为免费）</label><input id="lot-fee" type="number" min="0" max="100000" step="1" value="${draft.entry_fee || 0}"><p class="tiny muted" id="lot-fee-note">成功报名后扣除且不退（未中奖不退）。收费活动需已注册账户，不能搭配注册资格奖。</p></div>
          ` : ''}
          <div class="${isManual ? 'lot-field' : 'grid2'}">
            <div class="lot-field"><label for="lot-mode">开奖方式</label><select id="lot-mode">${options([['timed','定时开奖'],['count','满员开奖'],['manual','手动开奖']], draft.draw_mode)}</select></div>
            ${isTimed ? `<div class="lot-field"><label for="lot-date">报名截止 · 北京时间</label><input id="lot-date" type="datetime-local" required value="${date}"></div>` : ''}
            ${isCount ? `<div class="lot-field"><label for="lot-max-entries">开奖人数（1–100000）</label><input id="lot-max-entries" type="number" min="1" max="100000" step="1" required value="${draft.max_entries != null ? draft.max_entries : 100}"></div>` : ''}
          </div>
          <p class="tiny muted" id="lot-date-note">${isCount ? '满员开奖模式：满 X 人自动开奖，也可由管理员提前开奖。' : isManual ? '手动开奖模式：管理员开奖前均可报名。' : '定时开奖须填写截止时间，到点自动开奖。'}</p>

          ${isMemberOnly ? `
          <div class="subpanel">
            <h3>参与门槛</h3>
            <p class="tiny muted" style="margin-bottom:10px">留空或填 0 表示不限。设置任一门槛后，活动需已注册账户参与。</p>
            <div class="grid2">
              <div class="lot-field"><label for="lot-min-level">最低等级</label><select id="lot-min-level">${options(levelOpts, draft.min_level)}</select></div>
              <div class="lot-field"><label for="lot-min-points">最低积分余额</label><input id="lot-min-points" type="number" min="0" max="1000000" step="1" value="${draft.min_points || 0}"></div>
            </div>
            <div class="lot-field"><label for="lot-min-watch">近 30 天观看时长（小时）</label><input id="lot-min-watch" type="number" min="0" max="720" step="1" value="${draft.min_watch_hours || 0}"></div>
          </div>
          ` : ''}

          <div class="subpanel">
            ${isTimed ? `
            <label class="lot-check" for="lot-reminder"><input id="lot-reminder" type="checkbox" ${draft.reminder ? 'checked' : ''}><span>截止前 1 小时群内提醒（需设截止时间）</span></label>
            <p class="tiny muted">报名截止前约 1 小时自动在群内发送提醒公告。</p>
            ` : ''}
            <label class="lot-check" for="lot-pin" style="${isTimed ? 'margin-top:10px' : ''}"><input id="lot-pin" type="checkbox" ${draft.pin ? 'checked' : ''}><span>发布后静默置顶公告</span></label>
            <p class="tiny muted">发布后在群内静默置顶发起公告，开奖或取消后自动取消置顶。</p>
            <label class="lot-check" for="lot-anti-multi" style="margin-top:10px"><input id="lot-anti-multi" type="checkbox" ${draft.anti_multi ? 'checked' : ''}><span>严格防小号（关联账号均不中奖）</span></label>
            <p class="tiny muted">开启后，关联账号全部不中奖；关联记录仅管理员可见。</p>
            ${isLegacyDeviceChecked ? `
            <div id="lot-device-check-wrap" style="margin-top:10px;margin-left:14px;padding-left:10px;border-left:2px solid var(--line);">
              <label class="lot-check" for="lot-device-check" style="opacity:0.8"><input id="lot-device-check" type="checkbox" checked disabled><span>历史设备校验设置（只读）</span></label>
              <p class="tiny muted">仅严格防小号开启时生效；新活动不再使用。</p>
            </div>` : ''}
          </div>

          <h3 id="lot-prize-heading">奖池设置 · ${draft.prizes.reduce((s, p) => s + p.quantity, 0)} 个名额</h3>
          <div class="prize-list" id="lot-prize-list">${renderPrizeListHtml()}</div>
          <button type="button" class="add-prize" id="lot-add">＋ 添加一份奖励</button>
          <div id="lot-prize-editor"></div>
          <p class="error" id="lot-form-error" role="alert"></p>
          <div class="lot-actions lot-form-actions">
            <button type="button" class="btn secondary" id="lot-cancel">取消</button>
            <button type="submit" class="btn" id="lot-save">保存草稿</button>
          </div>
          <div class="lot-bottom-spacer"></div>
        </form>`);

      bind('#lot-cancel', back);
      bindPrizeRowActions();
      bind('#lot-add', () => {
        if ($('#lot-kind')) {
          $('#lot-form-error').textContent = '请先将编辑中的奖项加入奖池或取消该奖项';
          return;
        }
        openPrizeEditor(-1);
      });

      // Interactive linkage for draw_mode & audience
      const modeEl = $('#lot-mode');
      if (modeEl) {
        modeEl.onchange = () => {
          if ($('#lot-kind')) {
            $('#lot-form-error').textContent = '请先将编辑中的奖项加入奖池或取消该奖项，再切换开奖方式';
            modeEl.value = draft.draw_mode;
            return;
          }
          collect();
          draft.draw_mode = modeEl.value;
          if (draft.draw_mode === 'manual' || draft.draw_mode === 'count') {
            draft.deadline = null;
            draft.reminder = false;
          }
          if (draft.draw_mode !== 'count') {
            draft.max_entries = 0;
          } else if (draft.max_entries === undefined || draft.max_entries === null || draft.max_entries === 0) {
            draft.max_entries = 100;
          }
          renderEditor();
        };
      }
      const audEl = $('#lot-audience');
      if (audEl) {
        audEl.onchange = () => {
          if ($('#lot-kind')) {
            $('#lot-form-error').textContent = '请先将编辑中的奖项加入奖池或取消该奖项，再切换参与对象';
            audEl.value = draft.audience;
            return;
          }
          const prevAud = draft.audience;
          const target = audEl.value;
          if (target === 'member' && draft.prizes.some(p => p.kind === 'registration')) {
            $('#lot-form-error').textContent = '当前奖池包含本人专属注册资格奖，与「仅已注册」冲突；请先调整或移除该奖项后再切换';
            audEl.value = prevAud;
            return;
          }
          if (target === 'guest' && draft.prizes.some(p => ['points', 'invite', 'shop'].includes(p.kind))) {
            $('#lot-form-error').textContent = '当前奖池包含积分/邀请/商城等会员奖品，与「仅未注册」冲突；请先调整或移除该奖项后再切换';
            audEl.value = prevAud;
            return;
          }
          collect();
          draft.audience = target;
          if (draft.audience !== 'member') {
            draft.entry_fee = 0;
            draft.min_level = 0;
            draft.min_points = 0;
            draft.min_watch_hours = 0;
          }
          renderEditor();
        };
      }

      $('#lot-form').onsubmit = async e => {
        e.preventDefault(); const b = $('#lot-save'); b.disabled = true;
        try {
          if ($('#lot-kind')) throw new Error('请先将编辑中的奖项加入奖池或取消该奖项');
          collect();
          if (!draft.title) throw new Error('请输入抽奖活动名称');
          if (!draft.chat_id) throw new Error('请选择发布白名单群');
          if (!draft.deadline && draft.draw_mode === 'timed') throw new Error('定时开奖须设置报名截止时间');
          if (draft.draw_mode === 'count') {
            const raw = draft.max_entries;
            if (raw === '' || raw === null || raw === undefined || !Number.isInteger(Number(raw)) || Number(raw) < 1 || Number(raw) > 100000) {
              throw new Error('满员开奖人数须为 1–100000 之间的整数');
            }
          }
          if (draft.audience === 'member' && draft.prizes.some(p => p.kind === 'registration')) {
            throw new Error('仅已注册活动不能配置本人专属注册资格奖');
          }
          if (draft.audience === 'guest' && draft.prizes.some(p => ['points', 'invite', 'shop'].includes(p.kind))) {
            throw new Error('仅未注册活动不能配置积分、邀请名额或商城奖品；请选注册资格或人工奖品');
          }
          const hasGate = draft.min_level > 0 || draft.min_points > 0 || draft.min_watch_hours > 0;
          if (hasGate) {
            if (draft.audience === 'guest') throw new Error('设有参与门槛的活动不能选择仅未注册参与');
            if (draft.prizes.some(p => p.kind === 'registration')) throw new Error('设有参与门槛的活动不能配置本人专属注册资格奖');
          }
          if (draft.entry_fee > 0) {
            if (draft.audience === 'guest') throw new Error('收费活动需已注册积分账户，不能选择仅未注册参与');
            if (draft.prizes.some(p => p.kind === 'registration')) throw new Error('收费活动不能配置本人专属注册资格奖；请改为免费活动');
          }
          const d = await api(existing ? `${root}/${existing.id}` : root + '/admin/drafts', { method: existing ? 'PUT' : 'POST', body: { ...draft, revision: existing?.revision } });
          toast('草稿已保存');
          push(a => detail(d.id, a, 'manage'));
        } catch (err) { $('#lot-form-error').textContent = err.message; }
        finally { if (b.isConnected) b.disabled = false; }
      };
    }

    function collect() {
      const modeVal = $('#lot-mode') ? $('#lot-mode').value : draft.draw_mode;
      const audVal = $('#lot-audience') ? $('#lot-audience').value : draft.audience;
      const isMemberOnly = audVal === 'member';
      const isTimed = modeVal === 'timed';
      const isCount = modeVal === 'count';

      const feeEl = $('#lot-fee');
      const feeVal = feeEl ? Number(feeEl.value) : 0;
      const minLevelEl = $('#lot-min-level');
      const minLevelVal = minLevelEl ? Number(minLevelEl.value) || 0 : 0;
      const minPointsEl = $('#lot-min-points');
      const minPointsVal = minPointsEl ? Number(minPointsEl.value) || 0 : 0;
      const minWatchEl = $('#lot-min-watch');
      const minWatchVal = minWatchEl ? Number(minWatchEl.value) || 0 : 0;

      const dateEl = $('#lot-date');
      const dateVal = (isTimed && dateEl) ? dateEl.value : '';
      const reminderEl = $('#lot-reminder');
      const reminderVal = (isTimed && reminderEl) ? reminderEl.checked : false;
      const countEl = $('#lot-max-entries');
      let countVal = draft.max_entries;
      if (isCount && countEl) {
        const trimmed = countEl.value.trim();
        if (trimmed === '') {
          countVal = '';
        } else {
          const num = Number(trimmed);
          countVal = Number.isInteger(num) ? num : trimmed;
        }
      } else if (!isCount) {
        countVal = 0;
      }

      const antiMultiEl = $('#lot-anti-multi');
      const antiMultiVal = antiMultiEl ? antiMultiEl.checked : draft.anti_multi;
      const pinEl = $('#lot-pin');
      const pinVal = pinEl ? pinEl.checked : draft.pin;
      const titleEl = $('#lot-title');
      const titleVal = titleEl ? titleEl.value.trim() : draft.title;
      const chatEl = $('#lot-chat');
      const chatVal = chatEl ? chatEl.value : draft.chat_id;
      const threadEl = $('#lot-thread');
      const threadVal = threadEl && threadEl.value ? Number(threadEl.value) : null;

      Object.assign(draft, {
        anti_multi: antiMultiVal,
        device_check: isLegacyDeviceChecked,
        reminder: reminderVal,
        pin: pinVal,
        title: titleVal,
        chat_id: chatVal,
        thread_id: threadVal,
        audience: audVal,
        entry_fee: isMemberOnly && Number.isInteger(feeVal) && feeVal >= 0 ? feeVal : 0,
        draw_mode: modeVal,
        max_entries: isCount ? countVal : 0,
        min_level: isMemberOnly ? minLevelVal : 0,
        min_points: isMemberOnly ? minPointsVal : 0,
        min_watch_hours: isMemberOnly ? minWatchVal : 0,
        deadline: (isTimed && dateVal) ? Math.floor(new Date(dateVal + ':00+08:00').getTime() / 1000) : null
      });
    }

    function renderPrizeListHtml() {
      const gripSvg = '<svg viewBox="0 0 10 16" aria-hidden="true"><circle cx="3" cy="2" r="1.5"/><circle cx="7" cy="2" r="1.5"/><circle cx="3" cy="8" r="1.5"/><circle cx="7" cy="8" r="1.5"/><circle cx="3" cy="14" r="1.5"/><circle cx="7" cy="14" r="1.5"/></svg>';
      const total = draft.prizes.length;
      return draft.prizes.map((p, i) => `<div class="prize-row" id="lot-prize-row-${i}" data-index="${i}">
        <div class="prize-handle" data-lot-drag="${i}" title="按住拖拽排序" aria-label="按住拖动排序" role="button" tabindex="0">${gripSvg}</div>
        ${image(art[p.kind], 30)}
        <div class="grow">
          <b>${esc(p.name)}</b>${prizeTag(p, true)}
          <div class="prize-desc">${p.quantity} 名 · ${description(p)}</div>
        </div>
        <div class="prize-row-actions">
          ${i > 0 ? `<button type="button" class="prize-order-btn" data-lot-move-up="${i}" title="上移" aria-label="上移${esc(p.name)}">▲</button>` : ''}
          ${i < total - 1 ? `<button type="button" class="prize-order-btn" data-lot-move-down="${i}" title="下移" aria-label="下移${esc(p.name)}">▼</button>` : ''}
          <button type="button" class="text-btn" data-lot-edit="${i}" aria-label="编辑${esc(p.name)}">编辑</button>
          <button type="button" class="text-btn danger" data-lot-remove="${i}" aria-label="移除${esc(p.name)}">移除</button>
        </div>
      </div>`).join('') || '<div class="empty">添加第一份奖励</div>';
    }

    function syncPrizeListDom() {
      const heading = $('#lot-prize-heading');
      if (heading) heading.textContent = `奖池设置 · ${draft.prizes.reduce((s, p) => s + p.quantity, 0)} 个名额`;
      const listEl = $('#lot-prize-list');
      if (listEl) {
        listEl.innerHTML = renderPrizeListHtml();
        bindPrizeRowActions();
      }
    }

    function bindPrizeRowActions() {
      bind('[data-lot-edit]', b => {
        if ($('#lot-kind')) {
          $('#lot-form-error').textContent = '请先将编辑中的奖项加入奖池或取消该奖项';
          return;
        }
        openPrizeEditor(Number(b.dataset.lotEdit));
      });
      bind('[data-lot-remove]', b => {
        if ($('#lot-kind')) {
          $('#lot-form-error').textContent = '请先将编辑中的奖项加入奖池或取消该奖项';
          return;
        }
        const idx = Number(b.dataset.lotRemove);
        draft.prizes.splice(idx, 1);
        syncPrizeListDom();
      });
      bind('[data-lot-move-up]', b => {
        if ($('#lot-kind')) {
          $('#lot-form-error').textContent = '请先将编辑中的奖项加入奖池或取消该奖项';
          return;
        }
        const idx = Number(b.dataset.lotMoveUp);
        if (idx > 0) {
          const item = draft.prizes.splice(idx, 1)[0];
          draft.prizes.splice(idx - 1, 0, item);
          syncPrizeListDom();
        }
      });
      bind('[data-lot-move-down]', b => {
        if ($('#lot-kind')) {
          $('#lot-form-error').textContent = '请先将编辑中的奖项加入奖池或取消该奖项';
          return;
        }
        const idx = Number(b.dataset.lotMoveDown);
        if (idx < draft.prizes.length - 1) {
          const item = draft.prizes.splice(idx, 1)[0];
          draft.prizes.splice(idx + 1, 0, item);
          syncPrizeListDom();
        }
      });

      // Pointer-based touch and mouse drag and drop
      $$('#lot-prize-list [data-lot-drag]').forEach(handle => {
        handle.onpointerdown = e => {
          if ($('#lot-kind')) {
            $('#lot-form-error').textContent = '请先将编辑中的奖项加入奖池或取消该奖项';
            return;
          }
          // Only primary pointer and left-click / single-touch (ignore secondary touches / right click)
          if (!e.isPrimary || (e.pointerType === 'mouse' && e.button !== 0)) return;

          const fromIdx = Number(handle.dataset.lotDrag);
          const row = $(`#lot-prize-row-${fromIdx}`);
          if (!row) return;

          const activePointerId = e.pointerId;
          try { handle.setPointerCapture(activePointerId); } catch (_) {}
          row.classList.add('dragging');
          let currentTargetIdx = fromIdx;
          let cleanupDone = false;

          const removeListeners = () => {
            handle.removeEventListener('pointermove', onPointerMove);
            handle.removeEventListener('pointerup', onPointerUp);
            handle.removeEventListener('pointercancel', onPointerCancel);
            handle.removeEventListener('lostpointercapture', onPointerCancel);
          };

          const cleanupUi = () => {
            if (cleanupDone) return;
            cleanupDone = true;
            removeListeners();
            row.classList.remove('dragging');
            $$('#lot-prize-list .prize-row').forEach(r => r.classList.remove('drag-over'));
            try {
              if (handle.hasPointerCapture && handle.hasPointerCapture(activePointerId)) {
                handle.releasePointerCapture(activePointerId);
              }
            } catch (_) {}
          };

          const onPointerMove = moveEvent => {
            if (moveEvent.pointerId !== activePointerId) return;
            const listEl = $('#lot-prize-list');
            if (!listEl) return;
            const rows = $$('.prize-row', listEl);
            let foundIdx = fromIdx;
            for (let i = 0; i < rows.length; i++) {
              const r = rows[i];
              const rect = r.getBoundingClientRect();
              if (moveEvent.clientY >= rect.top && moveEvent.clientY <= rect.bottom) {
                foundIdx = Number(r.dataset.index);
                break;
              }
            }
            if (foundIdx !== currentTargetIdx) {
              rows.forEach(r => r.classList.remove('drag-over'));
              currentTargetIdx = foundIdx;
              const targetRow = $(`#lot-prize-row-${currentTargetIdx}`);
              if (targetRow && currentTargetIdx !== fromIdx) {
                targetRow.classList.add('drag-over');
              }
            }
          };

          const onPointerUp = upEvent => {
            if (upEvent.pointerId !== activePointerId) return;
            const targetIdx = currentTargetIdx;
            cleanupUi();
            // Valid pointerup: commit reorder if index changed
            if (targetIdx !== fromIdx && targetIdx >= 0 && targetIdx < draft.prizes.length) {
              const moved = draft.prizes.splice(fromIdx, 1)[0];
              draft.prizes.splice(targetIdx, 0, moved);
              syncPrizeListDom();
            }
          };

          const onPointerCancel = cancelEvent => {
            if (cancelEvent && cancelEvent.pointerId !== undefined && cancelEvent.pointerId !== activePointerId) return;
            // Cancel / lostpointercapture: clean up and preserve original order, do not reorder
            cleanupUi();
          };

          handle.addEventListener('pointermove', onPointerMove);
          handle.addEventListener('pointerup', onPointerUp);
          handle.addEventListener('pointercancel', onPointerCancel);
          handle.addEventListener('lostpointercapture', onPointerCancel);
        };
      });
    }

    function openPrizeEditor(editIndex = -1) {
      $('#lot-form-error').textContent = '';
      const isEditing = editIndex >= 0 && editIndex < draft.prizes.length;
      const initial = isEditing ? draft.prizes[editIndex] : null;
      const aud = draft.audience;
      // Filter prize kinds by audience
      const allowedKinds = Object.entries(kinds).filter(([k]) => {
        if (aud === 'member') return k !== 'registration';
        if (aud === 'guest') return k === 'registration' || k === 'manual';
        return true;
      });

      const initialKind = (initial && allowedKinds.some(([k]) => k === initial.kind))
        ? initial.kind
        : (allowedKinds[0] ? allowedKinds[0][0] : 'manual');
      const initialQty = initial ? initial.quantity : 1;

      const editorContainer = $('#lot-prize-editor');
      if (!editorContainer) return;
      editorContainer.innerHTML = `<div class="subpanel">
        <h3>${isEditing ? '编辑奖项' : '添加奖项'}</h3>
        <div class="lot-field"><label for="lot-kind">奖励类型</label><select id="lot-kind">${allowedKinds.map(([v, t]) => `<option value="${v}" ${v === initialKind ? 'selected' : ''}>${t}</option>`).join('')}</select></div>
        <div id="lot-kind-fields"></div>
        <div class="lot-field"><label for="lot-quantity">中奖人数</label><input type="number" id="lot-quantity" min="1" max="200" step="1" value="${initialQty}"></div>
        <p class="error" id="lot-prize-error" role="alert"></p>
        <div class="row">
          <button type="button" class="btn secondary" id="lot-prize-cancel">取消</button>
          <button type="button" class="btn" id="lot-prize-ok">${isEditing ? '保存修改' : '加入奖池'}</button>
        </div>
      </div>`;

      // Smooth scroll editorContainer into view within #screen
      editorContainer.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

      let currentKind = initialKind;
      const renderKindFields = (preset) => {
        const k = $('#lot-kind').value;
        currentKind = k;
        if (k === 'shop') {
          const selItemId = preset && preset.kind === 'shop' ? String(preset.item_id) : (opts.items[0]?.id || '');
          $('#lot-kind-fields').innerHTML = `<div class="lot-field"><label for="lot-item">当前商城商品（每人一件）</label><select id="lot-item">${opts.items.map(i => `<option value="${i.id}" ${String(i.id) === String(selItemId) ? 'selected' : ''}>${esc(i.name)}</option>`).join('')}</select></div>`;
        } else if (k === 'registration') {
          const selGroupId = preset && preset.kind === 'registration' ? String(preset.group_id) : (opts.groups[0]?.id || '');
          const days = preset && preset.kind === 'registration' ? (preset.days || 30) : 30;
          $('#lot-kind-fields').innerHTML = `<div class="lot-field"><label for="lot-group">账号分组</label><select id="lot-group">${opts.groups.map(g => `<option value="${esc(g.id)}" ${String(g.id) === String(selGroupId) ? 'selected' : ''}>${esc(g.name)}</option>`).join('')}</select></div><div class="lot-field"><label for="lot-days">有效天数</label><input id="lot-days" type="number" min="1" max="3650" value="${days}"></div>`;
        } else if (k === 'manual') {
          const name = preset && preset.kind === 'manual' ? (preset.name || '') : '';
          const instructions = preset && preset.kind === 'manual' ? (preset.instructions || '') : '';
          $('#lot-kind-fields').innerHTML = `<div class="lot-field"><label for="lot-prize-name">奖品名称（公开）</label><input id="lot-prize-name" maxlength="60" value="${esc(name)}"></div><div class="lot-field"><label for="lot-instructions">私密领取说明（仅中奖者/管理员）</label><textarea id="lot-instructions" maxlength="2000">${esc(instructions)}</textarea></div>`;
        } else {
          const amount = preset && preset.kind === k ? (preset.amount || (k === 'points' ? 500 : 1)) : (k === 'points' ? 500 : 1);
          $('#lot-kind-fields').innerHTML = `<div class="lot-field"><label for="lot-amount">每人获得的${k === 'points' ? '积分' : '可用邀请名额'}</label><input type="number" id="lot-amount" min="1" max="${k === 'points' ? 100000 : 100}" value="${amount}"></div>`;
        }
      };

      renderKindFields(initial);

      $('#lot-kind').onchange = () => {
        // If type changed from initial, don't pass stale preset values
        const changedKind = $('#lot-kind').value;
        const preset = (initial && initial.kind === changedKind) ? initial : null;
        renderKindFields(preset);
      };

      bind('#lot-prize-cancel', () => {
        editorContainer.innerHTML = '';
        if (isEditing) {
          const targetRow = $(`#lot-prize-row-${editIndex}`);
          if (targetRow) targetRow.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
      });

      bind('#lot-prize-ok', () => {
        const kind = $('#lot-kind').value;
        const quantity = Number($('#lot-quantity').value);
        const p = { kind, quantity };
        const fail = s => { $('#lot-prize-error').textContent = s; };

        collect();
        const hasGate = draft.min_level > 0 || draft.min_points > 0 || draft.min_watch_hours > 0;
        if ((draft.entry_fee > 0 || hasGate) && kind === 'registration') {
          return fail(hasGate ? '设置参与门槛后不能配置本人专属注册资格奖' : '收费活动不能配置本人专属注册资格奖；请将报名费用设为 0');
        }
        if (!Number.isInteger(quantity) || quantity < 1 || quantity > 200) {
          return fail('中奖人数应为 1–200 的整数');
        }

        if (kind === 'shop') {
          const item = opts.items.find(i => String(i.id) === $('#lot-item').value);
          if (!item) return fail('请先在商城启用商品');
          Object.assign(p, { item_id: item.id, name: item.name });
        } else if (kind === 'registration') {
          const g = opts.groups.find(g => g.id === $('#lot-group').value);
          const days = Number($('#lot-days').value);
          if (!g || !Number.isInteger(days) || days < 1 || days > 3650) return fail('请选择分组和 1–3650 的有效天数');
          Object.assign(p, { name: '本人专属注册资格', group_id: g.id, group_name: g.name, days });
        } else if (kind === 'manual') {
          const name = $('#lot-prize-name').value.trim();
          const instructions = $('#lot-instructions').value.trim();
          if (!name || !instructions) return fail('请填写名称和领取说明');
          Object.assign(p, { name, instructions });
        } else {
          const amount = Number($('#lot-amount').value);
          if (!Number.isInteger(amount) || amount < 1 || amount > (kind === 'points' ? 100000 : 100)) {
            return fail('奖励数量超出范围');
          }
          Object.assign(p, { amount, name: kind === 'points' ? `${amount} 积分` : `${amount} 个可用邀请名额` });
        }

        if (isEditing) {
          draft.prizes[editIndex] = p;
        } else {
          draft.prizes.push(p);
        }

        editorContainer.innerHTML = '';
        syncPrizeListDom();

        const targetIdx = isEditing ? editIndex : draft.prizes.length - 1;
        const targetRow = $(`#lot-prize-row-${targetIdx}`);
        if (targetRow) {
          targetRow.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
      });
    }
    renderEditor();
  }
  return { list, detail };
};
