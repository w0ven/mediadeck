/* Real lottery screens inside the member Mini App navigation/authentication. */
'use strict';
window.createLotteryUI = ({ api, esc, setScreen, push, back, toast, stamp, tg }) => {
  const root = '/api/app/lottery';
  const $ = (s) => document.querySelector(s);
  const image = (key, size = 32) => `<img class="emoji" width="${size}" height="${size}" src="/app/assets/lottery-assets/${key}.png" alt="">`;
  const lucky = () => `<div class="lucky" aria-label="LUCKY">${['L','U','C','K','Y'].map(k => image(k)).join('')}</div>`;
  const titles = { draft: '草稿', published: '报名中', settled: '已开奖' };
  const states = { pending: '待处理', delivered: '已发放', binding: '待绑定领取', manual: '待人工发放', failed: '失败待恢复', blocked: '待管理员处理', unknown: '需核验', sent: '已发送', sending: '发送中' };
  const kinds = { shop: '商城道具 · 自动入背包', points: '积分 · 直接入余额', invite: '可用邀请名额', registration: '本人专属注册资格', manual: '自定义奖品 · 人工发放' };
  const audience = { all: '全部群友', member: '仅已注册', guest: '仅未注册' };
  const art = { shop: 'gem', points: 'starCute', invite: 'rocket', registration: 'gift', manual: 'giftPurple' };
  const call = (path, body = {}) => api(root + path, { method: 'POST', body });
  let detailLoad = 0;
  function page(html) { setScreen(`<section class="lottery">${html}</section>`); }
  function browserId() {
    try {
      const key = 'mediadeck.lottery.browser.v1';
      let value = localStorage.getItem(key);
      if (!/^[0-9a-f]{64}$/.test(value || '')) {
        value = Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2,'0')).join('');
        localStorage.setItem(key, value);
      }
      if (localStorage.getItem(key) !== value) throw new Error('storage');
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
  const head = (title, side = '') => `<div class="row between"><div><div class="section-kicker">LUCKY STUDIO</div><h2>${esc(title)}</h2></div>${side}</div>`;

  function hero(c, state) {
    return `<div class="lot-hero">${lucky()}<h3>${esc(c.title)}</h3><img class="hero-art" src="/app/assets/lottery-assets/giftPink.png" alt="礼物"><img class="hero-star" src="/app/assets/lottery-assets/star.png" alt=""><div class="hero-bottom"><span>${stamp(c.deadline, true)} 截止 · 北京</span><span>${state === 'settled' ? '已开奖' : c.draw_mode === 'timed' ? '定时开奖' : '手动开奖'}</span></div></div>`;
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

  function prizes(c) {
    return `<div class="prize-list">${c.prizes.map(p => `<div class="prize-row"><div class="prize-icon">${image(art[p.kind],35)}</div><div class="grow"><div class="prize-name">${esc(p.name)}</div><div class="prize-desc">${description(p)}</div></div><div class="prize-count">${p.quantity} 名</div></div>`).join('')}</div>`;
  }

  function awardCard(a) {
    if (!a) return '';
    return `<div class="result-banner"><div class="row">${image(art[a.prize.kind],38)}<div><h3>好运已就绪！</h3><b>${esc(a.prize.name)}</b></div></div><p>${esc(states[a.state] || a.state)}${a.consumed ? ' · 资格已使用' : ''}${a.error ? '\n' + esc(a.error) : ''}${a.instructions ? '\n' + esc(a.instructions) : ''}</p>${a.state === 'binding' ? '<p class="small">请在 Bot 绑定或注册账号后领取。</p>' : ''}${a.prize.kind === 'shop' && a.state === 'delivered' ? '<p class="small">道具已入背包，使用后生效。</p>' : ''}${a.registration_url ? '<button class="btn" id="lot-register">进入 Bot 注册 · 本人专属</button>' : ''}${['binding','pending','failed'].includes(a.state) ? '<button class="btn" id="lot-claim">刷新领取奖励</button>' : ''}</div>`;
  }

  async function detail(lid, alive = () => true, mode = 'participant') {
    const load = ++detailLoad;
    const [ctx, d] = await Promise.all([api(root + '/context'), api(root + '/' + lid)]);
    if (!alive() || load !== detailLoad) return;
    const c = d.config, admin = ctx.is_admin;
    const isManage = admin && (mode === 'manage' || d.state === 'draft');
    const closed = d.state !== 'published' || Date.now() >= c.deadline * 1000;
    const eligible = c.audience === 'all' || (c.audience === 'member') === ctx.registered;
    const refresh = () => detail(lid, alive, isManage ? 'manage' : 'participant');
    const total = c.prizes.reduce((s, p) => s + p.quantity, 0);
    const fee = c.entry_fee || 0;
    const balance = ctx.points_balance;
    const hasEnough = !fee || (balance != null && balance >= fee);
    const hasRegPrize = c.prizes.some(p => p.kind === 'registration');

    // Header buttons
    const sideBtns = [];
    if (admin) {
      if (isManage) {
        sideBtns.push(`<button class="text-btn" id="lot-to-participant">参与模式</button>`);
      } else {
        sideBtns.push(`<button class="text-btn" id="lot-to-manage">管理本期</button>`);
      }
    }
    sideBtns.push(`<button class="text-btn" id="lot-refresh">刷新</button>`);

    if (isManage) {
      // Admin Management Mode
      page(`${head('抽奖中心 · 管理', sideBtns.join(''))}
        <div class="row between" style="margin:12px 0">
          <span class="pill">${image('shield', 16)}管理员管理模式</span>
          <span class="pill ${d.state === 'settled' ? 'gold' : 'green'}">${esc(titles[d.state])}${d.state === 'published' && closed ? ' · 已截止' : ''}</span>
        </div>
        ${hero(c, d.state)}
        <div class="lot-stats">
          <div class="stat"><b>${d.count}</b><span>参与人数</span></div>
          <div class="stat"><b>${total}</b><span>奖品名额</span></div>
          <div class="stat"><b>${d.state === 'settled' ? d.winners.length : '—'}</b><span>已中奖</span></div>
        </div>
        <div class="notice">
          <div class="row between">
            <span>费用：${fee ? fee + ' 积分' : '免费'} · ${esc(audience[c.audience])}</span>
            <span class="pill">${c.anti_multi ? '严格防小号' : '普通防刷'}</span>
          </div>
          <div style="margin-top:8px"><button class="btn secondary" id="lot-records">查看报名记录与关联异常</button></div>
        </div>
        <h3>本期奖池</h3>${prizes(c)}
        ${adminStatus(d)}
        <details class="lot-fold">
          <summary>活动规则与技术配置</summary>
          <div class="rule-note">${image('shield', 22)}<span>${esc(d.rules)}</span></div>
        </details>
        <div class="lot-actions">
          ${d.state === 'draft' ? '<button class="btn secondary" id="lot-edit">编辑草稿</button><button class="btn" id="lot-preview">发布前预览</button>' : d.state === 'published' ? '<button class="btn secondary" id="lot-list">活动列表</button><button class="btn" id="lot-draw">手动开奖</button>' : '<button class="btn secondary" id="lot-list">活动列表</button><button class="btn" id="lot-retry-awards">重试未发奖励</button>'}
        </div>
        <div class="footnote">管理员操作记录审计 · 开奖不可撤销</div>`);

      bind('#lot-to-participant', () => back());
      bind('#lot-refresh', refresh);
      bind('#lot-list', () => push(list));
      bind('#lot-edit', () => push(a => editor(a, d)));
      bind('#lot-preview', () => push(a => preview(lid, a)));
      bind('#lot-records', () => push(a => records(lid, a)));
      bind('#lot-draw', () => push(a => confirmation(a, '确认立即开奖？', `${c.title}\n${d.count} 人报名 · ${total} 个名额\n开奖后结果永久锁定。`, async () => { await call(`/${lid}/draw`); back(); })));
      bind('#lot-retry-awards', async () => { await call(`/${lid}/retry-awards`); await refresh(); });
      bind('[data-lot-confirm]', b => push(a => confirmation(a, '确认人工奖品已交付', '请先实际完成交付。确认后将记录发放状态。', async () => { await call(`/${lid}/awards/${b.dataset.lotConfirm}/confirm`); back(); })));
      bind('[data-lot-recover]', b => push(a => recover(lid, d.notices.find(n => String(n.id) === b.dataset.lotRecover), a)));
      return;
    }

    // Participant Mode (Default for all users, including Admin from group deep link)
    const joinText = d.joined ? '已报名 · 等待开奖' : closed ? '报名已截止' : !eligible ? '当前身份不可参与' : fee > 0 && balance == null ? '须绑定已注册账户' : fee > 0 && !hasEnough ? `积分不足（需 ${fee} 积分）` : fee > 0 ? `支付 ${fee} 积分报名` : '收下我的幸运号码';
    const canJoin = !closed && !d.joined && eligible && hasEnough && (fee === 0 || balance != null);

    page(`${head('福利放映室', sideBtns.join(''))}
      <div class="row between" style="margin:12px 0">
        <span class="pill">${image(admin ? 'shield' : ctx.registered ? 'verified' : 'starCute', 16)}${admin ? '管理员参与' : ctx.registered ? '已注册会员' : '未注册群友'}</span>
        <span class="pill ${d.state === 'settled' ? 'gold' : 'green'}">${esc(titles[d.state])}${d.state === 'published' && closed ? ' · 已截止' : ''}</span>
      </div>
      ${hero(c, d.state)}
      <div class="lot-stats">
        <div class="stat"><b>${d.count}</b><span>参与人数</span></div>
        <div class="stat"><b>${total}</b><span>奖品名额</span></div>
        <div class="stat"><b>${d.state === 'settled' ? d.winners.length : '—'}</b><span>已中奖</span></div>
      </div>
      ${fee > 0 ? `<div class="ticket"><div><strong>报名费用：${fee} 积分</strong><small>我的余额：${balance != null ? balance + ' 积分' : '未注册'} · 成功报名不退（未中奖不退）</small></div></div>` : ''}
      ${d.my_award ? awardCard(d.my_award) : ''}
      ${d.state === 'settled' && !d.my_award ? `<div class="result-banner"><h3>${d.joined ? '这次与好运擦肩而过' : '本期抽奖已结束'}</h3><p class="small">感谢参与，期待下期好运！</p></div>` : ''}
      ${d.joined && d.state !== 'settled' ? `<div class="ticket">${image('verified', 28)}<div><strong>报名已记录</strong><small>每人每期仅一次 · 等待开奖</small></div></div>` : ''}
      ${!ctx.registered && fee === 0 ? '<div class="rule-note">未注册群友可免费报名。中奖后按提示在 Bot 绑定或注册即可领取；本人专属注册资格绑定中奖者本人，不可转赠。</div>' : ''}
      ${ctx.registered && hasRegPrize ? '<div class="rule-note">已注册会员可报名本期其他奖项；本人专属注册资格仅在未注册者中抽取。</div>' : ''}
      <h3>本期奖池 <span class="muted tiny">/ ${esc(audience[c.audience])}</span></h3>${prizes(c)}
      ${d.state === 'settled' ? `<h3>本期幸运名单 · ${d.winners.length} 位</h3><div class="prize-list">${d.winners.map((w, i) => `<div class="winner"><span class="muted tiny">${i + 1}</span><div class="avatar">${esc(w.label.slice(0, 1))}</div><div class="grow"><b>${esc(w.label)}</b><div class="prize-desc">${esc(w.prize.name)}</div></div>${image('party', 24)}</div>`).join('') || '<div class="empty">本期未产生中奖者</div>'}</div>` : ''}
      <details class="lot-fold">
        <summary>查看活动规则与说明</summary>
        <div class="rule-note">${image('shield', 22)}<span>${esc(d.rules)}</span></div>
      </details>
      <div class="lot-actions">
        ${d.state !== 'settled' ? `<button class="btn" id="lot-join" ${canJoin ? '' : 'disabled'}>${joinText}</button>` : '<button class="btn secondary" id="lot-result-refresh">刷新领奖状态</button>'}
      </div>
      <div class="footnote">Telegram 签名身份 · 公平抽取 · 每人至多一奖</div>`);

    if (admin) bind('#lot-to-manage', () => push(a => detail(lid, a, 'manage')));
    bind('#lot-refresh,#lot-result-refresh', refresh);
    bind('#lot-claim', async () => { await call(`/${lid}/claim`); await refresh(); });
    bind('#lot-register', () => openBot(d.my_award.registration_url));
    bind('#lot-join', async () => {
      if (fee > 0) {
        push(a => confirmation(a, '确认扣费报名', `报名将扣除 ${fee} 积分。\n当前积分余额：${balance} 积分。\n成功报名后不退（未中奖不退），每人仅限一次机会。`, async () => {
          await call(`/${lid}/join`, { browser_id: browserId(), expected_entry_fee: fee });
          toast('报名已记录');
          back();
        }));
      } else {
        await call(`/${lid}/join`, { browser_id: browserId(), expected_entry_fee: 0 });
        toast('报名已记录');
        await refresh();
      }
    });
  }

  function adminStatus(d) {
    return `<h3>管理与发放状态</h3>${d.last_error ? `<p class="error">${esc(d.last_error)}</p>` : ''}${(d.notices || []).map(n => `<div class="notice"><div class="row between"><b>${n.kind === 'launch' ? '群发起公告' : '群开奖公示'}</b><span class="pill">${esc(states[n.state] || n.state)}</span></div><p class="tiny muted">${esc(n.error)}${n.message_id ? ` · 消息 #${n.message_id}` : ''}</p>${['failed','unknown'].includes(n.state) ? `<button class="btn secondary" data-lot-recover="${n.id}">核验 / 恢复公告</button>` : ''}</div>`).join('')}${(d.awards || []).map(a => `<div class="notice"><div class="row between"><b>${esc(a.label)}</b><span class="pill">${esc(states[a.state] || a.state)}</span></div><p class="small">${esc(a.prize.name)}</p>${a.instructions ? `<p class="small">私密说明：${esc(a.instructions)}</p>` : ''}${a.error ? `<p class="error">${esc(a.error)}</p>` : ''}${a.prize.kind === 'manual' && a.state !== 'delivered' ? `<button class="btn secondary" data-lot-confirm="${a.id}">确认已人工发放</button>` : ''}</div>`).join('')}`;
  }

  async function records(lid, alive = () => true, offset = 0) {
    const d = await api(`${root}/${lid}/records?offset=${offset}`);
    if (!alive()) return;
    const outcomes = { excluded: '异常排除 · 不参与中奖', winner: '已中奖', not_selected: '未抽中', eligible: '待开奖 · 资格核验中' };
    const reason = k => k === 'ip' ? '相同可信公网 IP' : '相同浏览器安装标识';
    page(`${head('报名与异常记录', '<button class="text-btn" id="lot-record-refresh">刷新</button>')}<div class="lot-stats"><div class="stat"><b>${d.total}</b><span>报名记录</span></div><div class="stat"><b>${d.excluded}</b><span>异常排除</span></div><div class="stat"><b>${d.clusters.length}</b><span>关联簇</span></div></div><h3>关联异常账号</h3>${d.clusters.map((c,i) => `<section class="notice lot-cluster"><div class="row between"><b>关联簇 ${i+1} · ${c.accounts.length} 个账号</b><span class="pill gold">全部排除</span></div><p class="small">${c.reasons.map(reason).join(' / ')}</p>${c.accounts.map(a => `<div class="winner"><div class="avatar">${esc(a.label.slice(0,1))}</div><div class="grow"><b>${esc(a.label)}</b><div class="tiny muted">TG ${esc(a.tg_id)} · ${a.excluded ? '本期不参与中奖' : '需刷新核验'}</div></div></div>`).join('')}<details class="lot-fold"><summary class="text-btn">查看关联证据</summary>${c.links.map(e => `<div class="rule-note">${reason(e.reason)} · TG ${e.tg_ids.map(esc).join('、')}<br>${stamp(e.detected_at,true)} 检测到关联</div>`).join('')}</details></section>`).join('') || '<div class="empty">本期无关联异常账号</div>'}<h3>全部报名记录 · ${esc(titles[d.state])}</h3><div class="prize-list">${d.entries.map(e => `<div class="prize-row"><div class="grow"><div class="prize-name">${esc(e.label)} <span class="tiny muted">TG ${esc(e.tg_id)}</span></div><div class="prize-desc">${stamp(e.joined_at,true)} 报名 · ${e.registered ? '当时已注册' : '当时未注册'}</div><div class="small ${e.excluded ? 'error' : 'muted'}">${outcomes[e.outcome]}${e.award_state ? ' · ' + esc(states[e.award_state] || e.award_state) : ''}</div></div></div>`).join('') || '<div class="empty">暂无报名</div>'}</div><div class="lot-actions"><button class="btn secondary" id="lot-record-back">返回本期活动</button>${offset ? '<button class="btn secondary" id="lot-record-prev">上一页</button>' : ''}${d.more ? '<button class="btn secondary" id="lot-record-next">下一页</button>' : ''}</div>`);
    bind('#lot-record-back', back);
    bind('#lot-record-refresh', () => records(lid, alive, offset));
    bind('#lot-record-prev', () => records(lid, alive, Math.max(0, offset - 50)));
    bind('#lot-record-next', () => records(lid, alive, offset + 50));
  }

  async function list(alive = () => true, offset = 0) {
    const data = await api(root + '/admin/list?offset=' + offset);
    if (!alive()) return;
    page(`${head('抽奖中心', '<button class="btn secondary" id="lot-new">＋ 新建抽奖</button>')}<p class="muted small">${image('shield', 20)} 管理入口仅服务端确认的管理员可见</p>${data.items.map((d, i) => `<button class="activity-item" data-lot-open="${esc(d.id)}">${image(['giftPink','giftBlue','giftPurple'][i%3],42)}<div class="grow"><b>${esc(d.title)}</b><div class="prize-desc">${stamp(d.deadline, true)} 截止 · 北京</div></div><span class="pill">${esc(titles[d.state])}</span></button>`).join('') || `<div class="empty">${image('gift', 64)}<p>暂无活动，点击上方新建。</p></div>`}<div class="lot-actions">${offset ? '<button class="btn secondary" id="lot-prev">上一页</button>' : ''}${data.more ? '<button class="btn secondary" id="lot-next">下一页</button>' : ''}</div>`);
    bind('#lot-new', () => push(editor));
    bind('[data-lot-open]', b => push(a => detail(b.dataset.lotOpen, a, 'manage')));
    bind('#lot-prev', () => list(alive, Math.max(0, offset - 50)));
    bind('#lot-next', () => list(alive, offset + 50));
  }

  function confirmation(alive, title, message, action) {
    if (!alive()) return;
    page(`${head(title)}<div class="empty">${image('giftPink', 78)}</div><p style="white-space:pre-wrap;line-height:1.8">${esc(message)}</p><div class="lot-actions"><button class="btn secondary" id="lot-cancel">取消</button><button class="btn" id="lot-ok">确认</button></div>`);
    bind('#lot-cancel', back);
    bind('#lot-ok', action);
  }

  async function preview(lid, alive) {
    const [d, p] = await Promise.all([api(root + '/' + lid), call(`/${lid}/preview`)]);
    if (!alive()) return;
    const copy = new DOMParser().parseFromString(p.text, 'text/html').body.textContent;
    page(`${head('发布前预览')}<p class="muted small">${image('megaphone', 22)} 以下正文将发往 ${esc(d.config.chat_id)}${d.config.thread_id ? ' · 话题 ' + d.config.thread_id : ''}</p><article class="message">${lucky()}<br>${esc(copy)}</article><div class="rule-note">发布后活动规则、奖项与截止时间全部锁定。</div><div class="lot-actions"><button class="btn secondary" id="lot-cancel">返回修改</button><button class="btn" id="lot-publish">确认发布到群</button></div>`);
    bind('#lot-cancel', back);
    bind('#lot-publish', async () => { await call(`/${lid}/publish`, { revision: d.revision }); toast('发布已处理'); back(); });
  }

  function recover(lid, n, alive) {
    if (!alive()) return;
    page(`${head('核验群公告')}<div class="rule-note">${esc(n.error)}<br>超时可能已发送成功，请先查看目标群。若已存在，请填写该消息 ID，系统将编辑原消息。</div><div class="lot-field"><label for="lot-mid">已有公告消息 ID</label><input id="lot-mid" type="number" min="1" value="${n.message_id || ''}"></div><label class="lot-check" for="lot-absent"><input id="lot-absent" type="checkbox"><span>我已人工核实目标群中不存在该公告</span></label><div class="lot-actions lot-form-actions"><button class="btn secondary" id="lot-cancel">返回</button><button class="btn" id="lot-recover-ok">确认恢复</button></div>`);
    bind('#lot-cancel', back);
    bind('#lot-recover-ok', async () => {
      await call(`/${lid}/notices/${n.id}/recover`, { message_id: $('#lot-mid').value ? Number($('#lot-mid').value) : null, confirmed_absent: $('#lot-absent').checked });
      back();
    });
  }

  async function editor(alive = () => true, existing = null) {
    const opts = await api(root + '/admin/options');
    if (!alive()) return;
    const draft = structuredClone(existing ? existing.config : { title: '', chat_id: opts.chats[0] || '', thread_id: null, audience: 'all', draw_mode: 'timed', deadline: null, prizes: [], anti_multi: true, entry_fee: 0 });
    function renderEditor() {
      const date = draft.deadline ? new Date((draft.deadline + 8 * 3600) * 1000).toISOString().slice(0, 16) : '';
      const options = (rows, value) => rows.map(([id, name]) => `<option value="${esc(id)}" ${String(id) === String(value) ? 'selected' : ''}>${esc(name)}</option>`).join('');
      page(`${head(existing ? '编辑抽奖草稿' : '新建一场惊喜')}<form id="lot-form" class="lot-form"><div class="lot-field"><label for="lot-title">活动名称</label><input id="lot-title" required maxlength="60" value="${esc(draft.title)}" placeholder="给这次惊喜起个名字"></div><div class="lot-field"><label for="lot-chat">发布白名单群</label><select id="lot-chat" required>${options(opts.chats.map(c => [c, c]), draft.chat_id)}</select></div><div class="lot-field"><label for="lot-thread">话题 ID（仅话题群，可留空）</label><input id="lot-thread" type="number" min="1" value="${draft.thread_id || ''}"></div><div class="lot-field"><label for="lot-audience">谁可以参与</label><select id="lot-audience">${options(Object.entries(audience), draft.audience)}</select></div><div class="lot-field"><label for="lot-fee">报名费用（积分，0 为免费）</label><input id="lot-fee" type="number" min="0" max="100000" step="1" value="${draft.entry_fee || 0}"><p class="tiny muted" id="lot-fee-note">成功报名后扣除且不退（未中奖不退）。收费活动需已注册账户，不能搭配注册资格奖。</p></div><div class="grid2"><div class="lot-field"><label for="lot-mode">开奖方式</label><select id="lot-mode">${options([['timed','定时开奖'],['manual','手动开奖']], draft.draw_mode)}</select></div><div class="lot-field"><label for="lot-date">报名截止 · 北京时间</label><input id="lot-date" type="datetime-local" required value="${date}"></div></div><div class="subpanel"><label class="lot-check" for="lot-anti-multi"><input id="lot-anti-multi" type="checkbox" ${draft.anti_multi ? 'checked' : ''}><span>严格防小号（关联账号均不中奖）</span></label><p class="tiny muted">相同 IP 或浏览器标识的账号全部排除，发布后规则冻结。</p></div><h3>奖池设置 · ${draft.prizes.reduce((s, p) => s + p.quantity, 0)} 个名额</h3><div class="prize-list">${draft.prizes.map((p, i) => `<div class="prize-row">${image(art[p.kind], 30)}<div class="grow"><b>${esc(p.name)}</b><div class="prize-desc">${p.quantity} 名 · ${description(p)}</div></div><button type="button" class="text-btn" data-lot-remove="${i}" aria-label="移除${esc(p.name)}">移除</button></div>`).join('') || '<div class="empty">添加第一份奖励</div>'}</div><button type="button" class="add-prize" id="lot-add">＋ 添加一份惊喜</button><div id="lot-prize-editor"></div><p class="error" id="lot-form-error" role="alert"></p><div class="lot-actions lot-form-actions"><button type="button" class="btn secondary" id="lot-cancel">取消</button><button type="submit" class="btn" id="lot-save">保存草稿</button></div></form>`);
      bind('#lot-cancel', back);
      bind('[data-lot-remove]', b => { collect(); draft.prizes.splice(Number(b.dataset.lotRemove), 1); renderEditor(); });
      bind('#lot-add', () => { if ($('#lot-kind')) return; addPrize(); });
      $('#lot-form').onsubmit = async e => {
        e.preventDefault(); const b = $('#lot-save'); b.disabled = true;
        try {
          if ($('#lot-kind')) throw new Error('请先将编辑中的奖项加入奖池或取消该奖项');
          collect();
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
      const feeVal = Number($('#lot-fee').value);
      Object.assign(draft, {
        anti_multi: $('#lot-anti-multi').checked,
        title: $('#lot-title').value.trim(),
        chat_id: $('#lot-chat').value,
        thread_id: $('#lot-thread').value ? Number($('#lot-thread').value) : null,
        audience: $('#lot-audience').value,
        entry_fee: Number.isInteger(feeVal) && feeVal >= 0 ? feeVal : 0,
        draw_mode: $('#lot-mode').value,
        deadline: Math.floor(new Date($('#lot-date').value + ':00+08:00').getTime() / 1000)
      });
    }
    function addPrize() {
      $('#lot-prize-editor').innerHTML = `<div class="subpanel"><h3>添加奖项</h3><div class="lot-field"><label for="lot-kind">奖励类型</label><select id="lot-kind">${Object.entries(kinds).map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}</select></div><div id="lot-kind-fields"></div><div class="lot-field"><label for="lot-quantity">中奖人数</label><input type="number" id="lot-quantity" min="1" max="200" step="1" value="1"></div><p class="error" id="lot-prize-error" role="alert"></p><div class="row"><button type="button" class="btn secondary" id="lot-prize-cancel">取消奖项</button><button type="button" class="btn" id="lot-prize-ok">加入奖池</button></div></div>`;
      const fields = () => {
        const k = $('#lot-kind').value;
        $('#lot-kind-fields').innerHTML = k === 'shop' ? `<div class="lot-field"><label for="lot-item">当前商城商品（每人一件）</label><select id="lot-item">${opts.items.map(i => `<option value="${i.id}">${esc(i.name)}</option>`).join('')}</select></div>` : k === 'registration' ? `<div class="lot-field"><label for="lot-group">账号分组</label><select id="lot-group">${opts.groups.map(g => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join('')}</select></div><div class="lot-field"><label for="lot-days">有效天数</label><input id="lot-days" type="number" min="1" max="3650" value="30"></div>` : k === 'manual' ? '<div class="lot-field"><label for="lot-prize-name">奖品名称（公开）</label><input id="lot-prize-name" maxlength="60"></div><div class="lot-field"><label for="lot-instructions">私密领取说明（仅中奖者/管理员）</label><textarea id="lot-instructions" maxlength="2000"></textarea></div>' : `<div class="lot-field"><label for="lot-amount">每人获得的${k === 'points' ? '积分' : '可用邀请名额'}</label><input type="number" id="lot-amount" min="1" max="${k === 'points' ? 100000 : 100}" value="${k === 'points' ? 500 : 1}"></div>`;
      };
      fields(); $('#lot-kind').onchange = fields;
      bind('#lot-prize-cancel', () => { $('#lot-prize-editor').innerHTML = ''; });
      bind('#lot-prize-ok', () => {
        const kind = $('#lot-kind').value, quantity = Number($('#lot-quantity').value), p = { kind, quantity };
        const fail = s => { $('#lot-prize-error').textContent = s; };
        collect();
        if (draft.entry_fee > 0 && kind === 'registration') return fail('收费活动不能配置本人专属注册资格奖；请将报名费用设为 0');
        if (!Number.isInteger(quantity) || quantity < 1 || quantity > 200) return fail('中奖人数应为 1–200 的整数');
        if (kind === 'shop') { const item = opts.items.find(i => String(i.id) === $('#lot-item').value); if (!item) return fail('请先在商城启用商品'); Object.assign(p, { item_id: item.id, name: item.name }); }
        else if (kind === 'registration') { const g = opts.groups.find(g => g.id === $('#lot-group').value), days = Number($('#lot-days').value); if (!g || !Number.isInteger(days) || days < 1 || days > 3650) return fail('请选择分组和 1–3650 有效天数'); Object.assign(p, { name: '本人专属注册资格', group_id: g.id, group_name: g.name, days }); }
        else if (kind === 'manual') { Object.assign(p, { name: $('#lot-prize-name').value.trim(), instructions: $('#lot-instructions').value.trim() }); if (!p.name || !p.instructions) return fail('请填写名称和领取说明'); }
        else { const amount = Number($('#lot-amount').value); if (!Number.isInteger(amount) || amount < 1 || amount > (kind === 'points' ? 100000 : 100)) return fail('奖励数量超出范围'); Object.assign(p, { amount, name: kind === 'points' ? `${amount} 积分` : `${amount} 个可用邀请名额` }); }
        draft.prizes.push(p); renderEditor();
      });
    }
    renderEditor();
  }
  return { list, detail };
};
