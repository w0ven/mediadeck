/* Invite accountability: pending inviter cases and each member's invite tree.
   Decisions here and on the group card share one backend path, so whichever
   side settles a case first wins and the other only shows the result. */
(function () {
  const STATUS = {
    active: ['正常', 'ok'], pending: ['待开通', 'idle'], expired: ['已到期', 'warn'],
    suspended: ['已停用', 'bad'], exhausted: ['已停用', 'bad'], deleted: ['已删除', 'bad'],
  };
  const CASE_STATE = {
    pending: ['待处理', 'warn'], processing: ['处理中', 'idle'], deleted: ['已连带删除', 'bad'],
    suspended: ['已暂停邀请', 'warn'], ignored: ['不处理', 'idle'], gone: ['邀请人已不在', 'idle'],
  };
  const day = (ts) => ts ? new Date(Number(ts) * 1000).toLocaleDateString('zh-CN') : '—';
  const stamp = (ts) => ts ? new Date(Number(ts) * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';
  const statusTag = (s) => { const [t, c] = STATUS[s] || [s || '未知', 'idle']; return `<span class="tag ${c}">${esc(t)}</span>`; };
  const caseTag = (s) => { const [t, c] = CASE_STATE[s] || [s, 'idle']; return `<span class="tag ${c}">${esc(t)}</span>`; };
  const who = (n) => `${esc(n.label || '未知账号')}${n.username ? ` <span class="muted">（${esc(n.username)}）</span>` : ''}`;
  const activity = (n) => n.status === 'deleted' || n.active_7d == null ? ''
    : (n.active_7d ? '<span class="tag ok">近7天活跃</span>' : '<span class="tag idle">近7天未观看</span>');

  async function decide(c, action, after) {
    const inviter = c.inviter || {};
    const name = `「${inviter.label || inviter.username}」（${inviter.username || inviter.emby_user_id}）`;
    if (action === 'delete') {
      if (!(await deckConfirm(`连带删除邀请人${name}？其 Emby 账号会一并删除，不可恢复。只处理这一层，不会再追究其上游。`,
        { title: '连带删除邀请人', confirmText: '删除邀请人', danger: true }))) return;
    } else if (action === 'suspend') {
      if (!(await deckConfirm(`暂停${name}的邀请资格 30 天？期间不能生成新邀请码，已发出的邀请码也暂时不能用；账号本身不受影响。`,
        { title: '暂停邀请资格', confirmText: '暂停 30 天' }))) return;
    }
    try {
      const r = await api(`/api/invite-cases/${encodeURIComponent(c.id)}/decide`, {
        method: 'POST', body: JSON.stringify({ action, confirm: action === 'delete' }) });
      if (r.already) toast('这个案件已经被处理过了');
      else if (r.error) toast('删除未完成：' + r.error, 1);
      else toast({ delete: '已连带删除邀请人', suspend: '已暂停邀请资格 30 天', ignore: '已标记为不处理' }[action]);
    } catch (e) { toast('操作失败：' + e.message, 1); }
    if (after) after();
  }

  function caseRow(c, after) {
    const d = c.deleted || {}, i = c.inviter || {};
    const open = c.status === 'pending';
    const actions = open ? `
      <button class="btn sm danger" type="button" data-case="${esc(c.id)}" data-act="delete">连带删除邀请人…</button>
      <button class="btn sm" type="button" data-case="${esc(c.id)}" data-act="suspend">暂停邀请 30 天…</button>
      <button class="btn sm" type="button" data-case="${esc(c.id)}" data-act="ignore">不处理</button>`
      : `<span class="muted">${esc(c.result || '')}${c.decided_at ? ' · ' + esc(stamp(c.decided_at)) : ''}</span>`;
    const delivery = c.delivery === 'sent' ? '' : `<div class="muted">群通知：${esc(c.delivery_error || ({ pending: '等待发送', skipped: '未发送', failed: '发送失败' }[c.delivery] || c.delivery))}</div>`;
    return `<tr>
      <td>#${esc(c.id)}<div class="muted">${esc(stamp(c.created_at))}</div></td>
      <td>${who(d)}<div class="muted">${esc(c.source_label || '')}</div></td>
      <td>${who(i)} ${statusTag(i.status)}<div class="muted">名下 ${esc(i.invitees == null ? '—' : i.invitees)} 人</div></td>
      <td>${caseTag(c.status)}${delivery}</td>
      <td class="row-actions">${actions}</td></tr>`;
  }

  function bindCases(host, cases, after) {
    host.querySelectorAll('[data-case]').forEach((btn) => {
      btn.onclick = async () => {
        const c = cases.find((x) => String(x.id) === btn.dataset.case);
        if (!c || btn.disabled) return;
        btn.disabled = true;
        try { await decide(c, btn.dataset.act, after); } finally { btn.disabled = false; }
      };
    });
  }

  /* Rendered at the top of 邀请与授权 (ops.js). */
  window.renderInviteCases = async function (host, onChange) {
    const all = await api('/api/invite-cases?status=all').catch(() => null);
    if (!host.isConnected) return;
    if (!all) { host.innerHTML = card('邀请连带处理', '', '<div class="empty">邀请案件读取失败，请刷新重试</div>'); return; }
    const cases = all.cases || [];
    const pending = cases.filter((c) => c.status === 'pending' || c.status === 'processing');
    const done = cases.filter((c) => !pending.includes(c)).slice(0, 10);
    const cols = ['案件', '被删账号', '邀请人', '状态', '处理'];
    host.innerHTML = card('邀请连带处理', pending.length ? `${pending.length} 个待处理` : '没有待处理案件', `
      <div class="card-body"><div class="help">管理员删号或违规规则自动删号后，被删账号的<b>直接邀请人</b>会出现在这里，Bot 也会在通知群发处理卡。
        在这里或群里处理都可以，以先处理的一方为准；只处理一层，不会再往上追究。到期删号和本人注销不产生案件。</div></div>
      <div class="card-body flush">${pending.length || done.length
        ? `<table><thead><tr>${cols.map((c) => `<th>${c}</th>`).join('')}</tr></thead><tbody>${pending.concat(done).map((c) => caseRow(c)).join('')}</tbody></table>`
        : '<div class="empty">还没有案件</div>'}</div>`);
    bindCases(host, pending, onChange || (() => window.renderInviteCases(host, onChange)));
  };

  function treeNodes(nodes, depth) {
    if (!nodes || !nodes.length) return depth ? '' : '<div class="empty">还没有邀请过别人</div>';
    return `<ul class="invite-tree">${nodes.map((n) => `
      <li><div class="invite-node">${statusTag(n.status)} <b>${esc(n.label)}</b>
        <span class="muted">${esc(n.username || '')}</span> ${activity(n)}
        <span class="muted">注册 ${esc(day(n.registered_at))}${n.status === 'deleted'
          ? ` · 删除 ${esc(day(n.deleted_at))}` : ` · 到期 ${n.expires_at ? esc(day(n.expires_at)) : '永久'}`}</span>
        ${(n.descendants || 0) && !(n.children || []).length ? `<span class="tag idle">下面还有 ${esc(n.descendants)} 人</span>` : ''}
      </div>${treeNodes(n.children, depth + 1)}</li>`).join('')}</ul>`;
  }

  /* Rendered into the member detail drawer by members.js. */
  window.renderInviteTree = async function (host, memberId) {
    host.innerHTML = '<div class="muted">加载中…</div>';
    let data;
    try { data = await api(`/api/members/${encodeURIComponent(memberId)}/invite-tree`); }
    catch (e) { if (host.isConnected) host.innerHTML = `<div class="empty">邀请关系读取失败：${esc(e.message)}</div>`; return; }
    if (!host.isConnected) return;
    const up = (data.upstream || []).slice().reverse();
    const pause = data.suspension;
    host.innerHTML = `
      <h4 class="hg-section-title">邀请链</h4>
      <div class="invite-chain">${up.length
        ? up.map((n) => `<span>${statusTag(n.status)} ${esc(n.label)} <span class="muted">${esc(n.username || '')}</span></span>`).join('<span class="muted"> → </span>') + '<span class="muted"> → </span><b>本账号</b>'
        : '<span class="muted">没有邀请人（管理员开通、卡密或旧账号）</span>'}</div>
      <h4 class="hg-section-title">邀请资格</h4>
      <div>${pause
        ? `<span class="tag warn">暂停至 ${esc(stamp(pause.until))}</span> <button class="btn sm" type="button" id="invite-lift">解除暂停</button>`
        : '<span class="tag ok">正常</span>'}</div>
      <h4 class="hg-section-title">TA 邀请的人 · 名下共 ${esc(data.descendants || 0)} 人</h4>
      ${treeNodes(data.downstream, 0)}
      ${(data.cases || []).length ? `<h4 class="hg-section-title">相关案件</h4>
        <div class="card-body flush"><table><thead><tr><th>案件</th><th>被删账号</th><th>邀请人</th><th>状态</th><th>处理</th></tr></thead>
        <tbody>${data.cases.map((c) => caseRow(c)).join('')}</tbody></table></div>` : ''}`;
    const reload = () => window.renderInviteTree(host, memberId);
    bindCases(host, (data.cases || []).filter((c) => c.status === 'pending'), reload);
    const lift = host.querySelector('#invite-lift');
    if (lift) lift.onclick = async () => {
      if (!(await deckConfirm('解除这个账号的邀请资格暂停？已发出的邀请码会恢复可用。', { title: '解除邀请暂停', confirmText: '解除暂停' }))) return;
      try { await api(`/api/members/${encodeURIComponent(memberId)}/invite-suspension`, { method: 'DELETE' }); toast('已解除暂停'); }
      catch (e) { toast('操作失败：' + e.message, 1); }
      reload();
    };
  };

  if (window.registerMemberDetailTab) {
    window.registerMemberDetailTab('invite-tree', '邀请树', (host, ctx) => { window.renderInviteTree(host, ctx.id); });
  }
})();
