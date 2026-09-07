/* User management page. Native JS, same theme as the rest of the panel.
   Does not wrap go() — reads location.hash. Shell go(route) keeps query in
   hash and passes pageContext; we only call go('members?…') when that API
   exists. Live updates use registerLiveUpdater(topic,payload,context) or a
   dedicated EventSource on the members topic (never renderPage). */
(function () {
  const COLS = ['', '账号 / TG', '用户组', '有效期', 'Emby / 同步', '用量', '活跃', ''];
  const TABS = [
    ['overview', '概况'],
    ['entitlements', '权益权限'],
    ['devices', '设备播放'],
    ['invites', '邀请积分'],
    ['audit', '操作记录'],
  ];
  const ms = {
    listing: null,
    groups: [],
    selected: new Set(),
    writingHash: false,
    liveSrc: null,
    liveBound: false,
  };

  function hasLiveShell() {
    return typeof pageContext === 'function' && typeof renderView === 'function';
  }

  function membersContext(liveUpdate) {
    if (typeof pageContext === 'function') return pageContext('members', !!liveUpdate);
    const version = (typeof state !== 'undefined' && state.renderVersion) || 0;
    return {
      page: 'members',
      route: (typeof state !== 'undefined' && state.route) || 'members',
      live: !!liveUpdate,
      isCurrent() {
        if (!$('#members-page') && !liveUpdate) return true;
        if (typeof state === 'undefined') return !!$('#members-page');
        const cur = String(state.page || '').split('?')[0];
        return cur === 'members' && (state.renderVersion || 0) === version;
      },
    };
  }

  function parseMembersHash(raw) {
    const text = String(raw == null ? (location.hash || '') : raw).replace(/^#\/?/, '');
    const q = text.indexOf('?');
    const path = (q >= 0 ? text.slice(0, q) : text).split('/')[0];
    const params = new URLSearchParams(q >= 0 ? text.slice(q + 1) : '');
    return { page: path || 'dashboard', params };
  }

  function membersQueryFromParams(params) {
    const p = new URLSearchParams();
    const map = {
      q: 'search', page: 'page', page_size: 'page_size', sort: 'sort',
      order: 'order', status: 'status', group_id: 'group_id', tg: 'tg',
      expiring: 'expiring', emby_status: 'emby_status', sync_status: 'sync_status',
    };
    Object.keys(map).forEach((k) => {
      const v = params.get(k);
      if (v) p.set(map[k], v);
    });
    if (!p.get('page')) p.set('page', '1');
    if (!p.get('page_size')) p.set('page_size', '50');
    return p;
  }

  function toastResult(r, okMsg) {
    if (r && r.ok === true) { toast(okMsg); return true; }
    const err = (r && (r.error || (r.errors && r.errors[0] && r.errors[0].error))) || '远端未成功';
    toast(err, 1);
    return false;
  }

  function currentParams() {
    return parseMembersHash().params;
  }

  function routeFromParams(params) {
    const qs = new URLSearchParams();
    params.forEach((v, k) => { if (v) qs.set(k, v); });
    return 'members' + (qs.toString() ? '?' + qs.toString() : '');
  }

  function writeHash(params, { navigate = false } = {}) {
    const route = routeFromParams(params);
    if (navigate && hasLiveShell() && typeof go === 'function') {
      go(route);
      return 'go';
    }
    ms.writingHash = true;
    const next = '#/' + route;
    if (location.hash !== next) history.replaceState(null, '', next);
    ms.writingHash = false;
    return 'local';
  }

  function setParam(key, value, { reload = true } = {}) {
    const params = currentParams();
    if (value == null || value === '') params.delete(key);
    else params.set(key, String(value));
    if (key !== 'page' && key !== 'id' && key !== 'tab') params.set('page', '1');
    if (key === 'tab') {
      writeHash(params, { navigate: false });
      const id = params.get('id');
      if (id) fillDetail(id, value || 'overview');
      return;
    }
    const mode = writeHash(params, { navigate: reload && key !== 'id' });
    if (key === 'id') {
      writeHash(params, { navigate: false });
      fillDetail(value, params.get('tab') || 'overview');
      return;
    }
    if (mode === 'local' && reload) loadMembers(membersContext(false));
  }

  function entitlementTag(m) {
    return stateTag(m.entitlement_state || m.state);
  }

  function embySyncCell(m) {
    const emby = m.emby_status || 'unknown';
    const sync = m.sync_status || 'unknown';
    const embyLabel = ({
      present: m.emby_disabled ? 'Emby 已禁用' : 'Emby 在线',
      missing: '账号缺失',
      unknown: 'Emby 未知',
    })[emby] || emby;
    const embyCls = emby === 'present' && !m.emby_disabled ? 'ok'
      : emby === 'missing' ? 'bad' : 'idle';
    const syncLabel = ({
      in_sync: '已同步', drift: '策略漂移', never_applied: '从未下发',
      failed: '同步失败', skipped_admin: '管理员跳过',
      emby_missing: '无法同步', unknown: '同步未知',
    })[sync] || sync;
    const syncCls = sync === 'in_sync' ? 'ok'
      : (sync === 'drift' || sync === 'failed' || sync === 'emby_missing') ? 'bad' : 'idle';
    const retry = m.retryable
      ? `<button class="btn sm" type="button" data-act="retry" data-id="${esc(m.emby_user_id)}">重试</button>`
      : '';
    return `<div><span class="tag ${embyCls}">${esc(embyLabel)}</span>
      <span class="tag ${syncCls}">${esc(syncLabel)}</span>${retry}</div>`;
  }

  function usageCell(m) {
    const quota = m.traffic_quota_bytes ? fmtBytes(m.traffic_used_bytes || 0)
      + ' / ' + fmtBytes(m.traffic_quota_bytes) : '配额 ' + fmtBytes(m.traffic_used_bytes || 0);
    const edge = m.edge || {};
    return `<div class="s">${esc(quota)}<div class="muted">直链 30 天 ${esc(fmtBytes(edge.bytes_30d || 0))}</div></div>`;
  }

  function accountCell(m) {
    const tg = m.tg_username ? '@' + m.tg_username : (m.tg_user_id ? '已绑定' : '');
    return `<button class="linkish" type="button" data-act="open" data-id="${esc(m.emby_user_id)}">${esc(m.username || m.emby_user_id)}</button>
      <div class="s muted">${esc(tg || '未绑定 TG')}</div>`;
  }

  function expiryCell(m) {
    const ts = m.expires_at_effective || m.expires_at;
    const label = ts ? fmtExpiry(ts) : '不限期';
    const extra = m.overridden_keys && m.overridden_keys.includes('expires_at_override')
      ? '<div class="muted">个人覆盖</div>' : '';
    return `${entitlementTag(m)} <span>${esc(label)}</span>${extra}`;
  }

  function rowHtml(m) {
    const id = m.emby_user_id;
    const checked = ms.selected.has(id) ? 'checked' : '';
    return `<tr data-live-key="member:${esc(id)}" data-id="${esc(id)}" tabindex="0">
      <td><input type="checkbox" class="m-pick" data-id="${esc(id)}" ${checked} aria-label="选择 ${esc(m.username)}"></td>
      <td>${accountCell(m)}</td>
      <td>${esc(m.group_name || '—')}</td>
      <td>${expiryCell(m)}</td>
      <td>${embySyncCell(m)}</td>
      <td>${usageCell(m)}</td>
      <td>${esc(m.last_activity ? fmtAgeTs(Date.parse(m.last_activity) / 1000) : (m.last_seen_at ? fmtAgeTs(m.last_seen_at) : '—'))}</td>
      <td class="row-actions">
        <button class="btn sm" type="button" data-act="open" data-id="${esc(id)}">详情</button>
        <button class="btn sm danger" type="button" data-act="delete" data-id="${esc(id)}" data-name="${esc(m.username)}">删除</button>
      </td>
    </tr>`;
  }

  function statsHtml(listing) {
    const c = listing.counts || {};
    return `<div class="stat-grid" id="members-stats">
      ${stat('☺', c.total || 0, '总用户', '全量筛选结果，不是当前页')}
      ${stat('✓', c.active || 0, '权益正常', 'entitlement，不是 Emby 播放')}
      ${stat('⌛', (c.expired || 0) + (c.exhausted || 0), '到期/用尽', `${c.expired || 0} 过期 · ${c.exhausted || 0} 用尽`)}
      ${stat('⚠', (c.emby_missing || 0) + (c.sync_drift || 0) + (c.sync_failed || 0), 'Emby/同步',
        `${c.emby_missing || 0} 缺失 · ${c.sync_drift || 0} 漂移 · ${c.sync_failed || 0} 失败`)}
    </div>`;
  }

  function pagerHtml(listing) {
    const page = listing.page || 1;
    const size = listing.page_size || 50;
    const total = listing.total || 0;
    const pages = Math.max(1, Math.ceil(total / size));
    return `<div class="members-pager" id="members-pager">
      <button class="btn sm" type="button" data-act="page" data-page="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一页</button>
      <span class="muted">第 ${esc(page)} / ${esc(pages)} 页 · ${esc(total)} 人</span>
      <button class="btn sm" type="button" data-act="page" data-page="${page + 1}" ${page >= pages ? 'disabled' : ''}>下一页</button>
      <label class="muted">每页
        <select id="m-page-size" aria-label="每页条数">
          ${[25, 50, 100].map((n) => `<option value="${n}" ${n === size ? 'selected' : ''}>${n}</option>`).join('')}
        </select>
      </label>
    </div>`;
  }

  function filterBar(params, groups) {
    const gopts = groups.map((g) =>
      `<option value="${esc(g.id)}" ${params.get('group_id') === g.id ? 'selected' : ''}>${esc(g.name)}</option>`).join('');
    return `<div class="toolbar members-filters" id="members-filters" data-live-preserve>
      <label>搜索 <input id="m-q" type="search" value="${esc(params.get('q') || '')}" placeholder="账号 / 备注 / 联系方式" aria-label="搜索用户"></label>
      <label>状态 <select id="m-status" aria-label="权益状态">
        <option value="">全部</option>
        ${['active', 'expired', 'exhausted', 'suspended', 'pending'].map((s) =>
          `<option value="${s}" ${params.get('status') === s ? 'selected' : ''}>${s}</option>`).join('')}
      </select></label>
      <label>用户组 <select id="m-group" aria-label="用户组"><option value="">全部</option>${gopts}</select></label>
      <label>Emby <select id="m-emby" aria-label="Emby 状态">
        <option value="">全部</option>
        ${['present', 'missing', 'unknown'].map((s) =>
          `<option value="${s}" ${params.get('emby_status') === s ? 'selected' : ''}>${s}</option>`).join('')}
      </select></label>
      <label>同步 <select id="m-sync" aria-label="同步状态">
        <option value="">全部</option>
        ${['in_sync', 'drift', 'failed', 'never_applied', 'emby_missing'].map((s) =>
          `<option value="${s}" ${params.get('sync_status') === s ? 'selected' : ''}>${s}</option>`).join('')}
      </select></label>
      <button class="btn" type="button" id="m-reset">重置筛选</button>
    </div>`;
  }

  function shellHtml(listing, params, groups) {
    const unmanaged = listing.unmanaged || [];
    const err = listing.unmanaged_error
      ? `<div class="help danger-text">Emby 列表不可用：${esc(listing.unmanaged_error)}</div>` : '';
    return `<div id="members-page">
      ${statsHtml(listing)}
      ${filterBar(params, groups)}
      <div class="toolbar" id="members-bulk">
        <label><input type="checkbox" id="m-pick-page"> 本页全选</label>
        <span class="muted" id="m-sel-count">已选 ${ms.selected.size} 人（跨页勾选不会操作未选用户）</span>
        <button class="btn sm" type="button" data-act="bulk" data-bulk="renew">续期</button>
        <button class="btn sm" type="button" data-act="bulk" data-bulk="suspend">停用</button>
        <button class="btn sm" type="button" data-act="enforce">策略预览</button>
      </div>
      ${err}
      <div class="card" id="members-table-card">
        <div class="card-head"><div><h3>用户</h3><div class="sub">${esc(listing.total || 0)} 人</div></div></div>
        <div class="card-body flush" id="members-table-wrap">
          ${(listing.members || []).length
            ? `<table id="members-table" class="member-table"><thead><tr>${COLS.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead>
               <tbody id="members-tbody">${(listing.members || []).map(rowHtml).join('')}</tbody></table>`
            : '<div class="empty" id="members-empty">没有符合筛选的用户</div>'}
        </div>
      </div>
      ${pagerHtml(listing)}
      <div id="member-detail" data-live-preserve class="${params.get('id') ? '' : 'hidden'}"></div>
      ${unmanaged.length ? `<div class="card" id="members-unmanaged"><div class="card-head"><div><h3>未纳管 Emby 账号</h3>
        <div class="sub">${esc(listing.unmanaged_total || unmanaged.length)} 个，对照全部已纳管 id</div></div></div>
        <div class="card-body flush"><table><thead><tr><th>账号</th><th>管理员</th><th></th></tr></thead><tbody>
          ${unmanaged.slice(0, 50).map((u) => `<tr data-live-key="unmanaged:${esc(u.emby_user_id)}">
            <td>${esc(u.username)}</td><td>${u.is_admin ? '是' : '否'}</td>
            <td>${u.is_admin ? '' : `<button class="btn sm" type="button" data-act="enrol" data-id="${esc(u.emby_user_id)}" data-name="${esc(u.username)}">纳入</button>`}</td>
          </tr>`).join('')}
        </tbody></table></div></div>` : ''}
    </div>`;
  }

  function bindFilters() {
    const q = $('#m-q');
    if (q && !q.dataset.bound) {
      q.dataset.bound = '1';
      q.onkeydown = (e) => {
        if (e.key === 'Enter') { e.preventDefault(); setParam('q', q.value.trim()); }
      };
    }
    [['m-status', 'status'], ['m-group', 'group_id'], ['m-emby', 'emby_status'],
      ['m-sync', 'sync_status']].forEach(([id, key]) => {
      const el = document.getElementById(id);
      if (el && !el.dataset.bound) {
        el.dataset.bound = '1';
        el.onchange = () => setParam(key, el.value);
      }
    });
    const reset = $('#m-reset');
    if (reset && !reset.dataset.bound) {
      reset.dataset.bound = '1';
      reset.onclick = () => {
        writeHash(new URLSearchParams({ page: '1', page_size: '50' }), { navigate: hasLiveShell() });
        if (!hasLiveShell()) loadMembers(membersContext(false));
      };
    }
    const size = $('#m-page-size');
    if (size) size.onchange = () => setParam('page_size', size.value);
    const pickPage = $('#m-pick-page');
    if (pickPage) {
      pickPage.onchange = () => {
        (ms.listing.members || []).forEach((m) => {
          if (pickPage.checked) ms.selected.add(m.emby_user_id);
          else ms.selected.delete(m.emby_user_id);
        });
        patchSelection();
      };
    }
  }

  function patchSelection() {
    const count = $('#m-sel-count');
    if (count) count.textContent = `已选 ${ms.selected.size} 人（跨页勾选不会操作未选用户）`;
    document.querySelectorAll('.m-pick').forEach((box) => {
      box.checked = ms.selected.has(box.dataset.id);
    });
  }

  function bindTable() {
    const root = $('#members-page');
    if (!root || root.dataset.bound) return;
    root.dataset.bound = '1';
    root.onclick = (e) => {
      const btn = e.target.closest('[data-act]');
      if (!btn) return;
      const act = btn.dataset.act;
      const id = btn.dataset.id;
      if (act === 'open') openDetail(id);
      if (act === 'delete') confirmDelete(id, btn.dataset.name);
      if (act === 'retry') retryRemote(id);
      if (act === 'page') setParam('page', btn.dataset.page);
      if (act === 'bulk') bulk(btn.dataset.bulk);
      if (act === 'enforce') showEnforcement();
      if (act === 'enrol') enrol(id, btn.dataset.name);
    };
    root.onchange = (e) => {
      const box = e.target.closest('.m-pick');
      if (!box) return;
      if (box.checked) ms.selected.add(box.dataset.id);
      else ms.selected.delete(box.dataset.id);
      patchSelection();
    };
    root.onkeydown = (e) => {
      const tr = e.target.closest('tr[data-id]');
      if (tr && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault();
        openDetail(tr.dataset.id);
      }
    };
  }

  function patchLocal(listing, params, groups) {
    const stats = $('#members-stats');
    if (stats) stats.outerHTML = statsHtml(listing);
    const tbody = $('#members-tbody');
    const wrap = $('#members-table-wrap');
    if (tbody && (listing.members || []).length) {
      tbody.innerHTML = (listing.members || []).map(rowHtml).join('');
    } else if (wrap) {
      wrap.innerHTML = (listing.members || []).length
        ? `<table id="members-table" class="member-table"><thead><tr>${COLS.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead>
           <tbody id="members-tbody">${(listing.members || []).map(rowHtml).join('')}</tbody></table>`
        : '<div class="empty" id="members-empty">没有符合筛选的用户</div>';
    }
    const pager = $('#members-pager');
    if (pager) pager.outerHTML = pagerHtml(listing);
    patchSelection();
    void params;
    void groups;
  }

  async function loadMembers(context) {
    context = context || membersContext(false);
    const params = currentParams();
    if (!context.live) {
      if (typeof renderView === 'function') renderView(pageLoading(), context);
      else $('#view').innerHTML = pageLoading();
    } else if (typeof isEditing === 'function' && isEditing()) {
      return;
    }
    try {
      const [listing, groups] = await Promise.all([
        api('/api/members?' + membersQueryFromParams(params).toString()),
        (context.live && ms.groups.length) ? Promise.resolve(ms.groups) : api('/api/groups'),
      ]);
      if (typeof context.isCurrent === 'function' && !context.isCurrent()) return;
      ms.listing = listing;
      ms.groups = groups;
      const html = shellHtml(listing, params, groups);
      const wrap = $('#members-table-wrap');
      const scroll = wrap ? wrap.scrollTop : 0;
      if (typeof renderView === 'function') {
        renderView(html, context);
      } else if (context.live && $('#members-page')) {
        patchLocal(listing, params, groups);
      } else {
        $('#view').innerHTML = html;
      }
      bindFilters();
      bindTable();
      patchSelection();
      const wrap2 = $('#members-table-wrap');
      if (wrap2) wrap2.scrollTop = scroll;
      const id = params.get('id');
      const host = $('#member-detail');
      if (id && !(context.live && host && host.dataset.uid === id)) {
        await fillDetail(id, params.get('tab') || 'overview');
      }
    } catch (e) {
      if (typeof context.isCurrent === 'function' && !context.isCurrent()) return;
      if (!context.live) {
        $('#view').innerHTML = pageError(e);
        const btn = $('#retry-page');
        if (btn) btn.onclick = () => loadMembers(membersContext(false));
        toast('加载失败: ' + e.message, 1);
      }
    }
  }

  function refreshNow() {
    return loadMembers(membersContext(true));
  }

  function openDetail(id) {
    const params = currentParams();
    params.set('id', id);
    if (!params.get('tab')) params.set('tab', 'overview');
    writeHash(params, { navigate: false });
    fillDetail(id, params.get('tab') || 'overview');
  }

  async function fillDetail(id, tab) {
    const host = $('#member-detail');
    if (!host) return;
    host.classList.remove('hidden');
    host.dataset.uid = id;
    host.innerHTML = '<div class="card"><div class="card-body">加载详情…</div></div>';
    try {
      const d = await api(`/api/members/${encodeURIComponent(id)}?days=30`);
      const m = d.member || {};
      const tabs = TABS.map(([k, label]) =>
        `<button class="tab ${k === tab ? 'active' : ''}" type="button" data-tab="${k}">${esc(label)}</button>`).join('');
      let body = '';
      if (tab === 'overview') {
        const gopts = (ms.groups || []).map((g) =>
          `<option value="${esc(g.id)}" ${g.id === m.group_id ? 'selected' : ''}>${esc(g.name)}</option>`).join('');
        body = `<dl class="member-kv">
          <dt>权益</dt><dd>${entitlementTag(m)} ${esc(m.state_reason || '')}</dd>
          <dt>Emby</dt><dd>${embySyncCell(m)}</dd>
          <dt>到期</dt><dd>${esc(fmtExpiry(m.expires_at_effective || m.expires_at))}</dd>
          <dt>配额用量</dt><dd>${esc(fmtBytes(m.traffic_used_bytes || 0))} / ${esc(m.traffic_quota_bytes ? fmtBytes(m.traffic_quota_bytes) : '不限')}</dd>
          <dt>直链 7/30/累计</dt><dd>${esc(fmtBytes((d.edge && d.edge.bytes_7d) || (m.edge || {}).bytes_7d || 0))}
            · ${esc(fmtBytes((d.edge && d.edge.bytes_30d) || (m.edge || {}).bytes_30d || 0))}
            · ${esc(fmtBytes((d.edge && d.edge.bytes_total) || (m.edge || {}).bytes_total || 0))}</dd>
        </dl>
        <div class="toolbar" id="md-actions">
          <label>续期 <input id="md-days" type="number" min="1" value="30" style="width:72px"> 天
            <button class="btn sm" type="button" id="md-renew">续期</button></label>
          <label>换组 <select id="md-group">${gopts}</select>
            <select id="md-policy">
              <option value="keep">保留有效期</option>
              <option value="apply_group">套用目标组天数</option>
              <option value="clear">改为不限期</option>
            </select>
            <button class="btn sm" type="button" id="md-group-go">换组</button></label>
          <button class="btn sm" type="button" id="md-retry">重试远端</button>
        </div>`;
      } else if (tab === 'entitlements') {
        body = `<p class="help">组 ${esc(m.group_name)} · 覆盖 ${esc((m.overridden_keys || []).join(', ') || '无')}</p>
          <pre class="members-pre">${esc(JSON.stringify(m.effective || {}, null, 2))}</pre>`;
      } else if (tab === 'devices') {
        const devices = d.devices || [];
        const plays = d.plays || d.recent_plays || [];
        body = devices.length
          ? `<table><thead><tr><th>设备</th><th>客户端</th><th></th></tr></thead><tbody>${devices.map((x) => `<tr>
              <td>${esc(x.device_name || x.device_id)}</td><td>${esc(x.client || '')}</td>
              <td><button class="btn sm" type="button" data-dev="${esc(x.device_id)}" data-block="${x.blocked ? '0' : '1'}">${x.blocked ? '解禁' : '封锁'}</button></td>
            </tr>`).join('')}</tbody></table>`
          : '<div class="empty">无设备记录</div>';
        body += `<h4>最近播放</h4>` + (plays.length
          ? `<ul>${plays.map((p) => `<li>${esc(p.item_name || p.Name || p.item || '—')}</li>`).join('')}</ul>`
          : '<div class="empty">暂无播放</div>');
      } else if (tab === 'invites') {
        body = `<p>积分 ${esc(d.points || 0)} · 邀请名额 ${esc(m.invite_quota || 0)} · 下级 ${esc(m.invitee_count || 0)}</p>
          <p class="help">求片剩余 ${esc(d.request_remaining == null ? '—' : d.request_remaining)}</p>
          ${(d.requests || []).length
            ? `<ul>${d.requests.map((r) => `<li>${esc(r.title || r.query || r.id)} · ${esc(r.status)}</li>`).join('')}</ul>`
            : '<div class="empty">无求片记录</div>'}`;
      } else {
        const audit = d.audit || [];
        body = audit.length
          ? `<table><thead><tr><th>时间</th><th>动作</th><th>详情</th><th></th></tr></thead><tbody>${audit.map((a) => `<tr>
              <td>${esc(fmtAgeTs(a.ts))}</td><td>${esc(a.action)}</td>
              <td>${esc(a.detail)}</td>
              <td>${a.ok ? '<span class="tag ok">成功</span>' : '<span class="tag bad">失败</span>'}</td>
            </tr>`).join('')}</tbody></table>`
          : '<div class="empty">无操作记录</div>';
      }
      host.innerHTML = `<div class="card member-detail-card">
        <div class="card-head"><div><h3>${esc(m.username)}</h3>
          <div class="sub">${esc(m.emby_user_id)}</div></div>
          <button class="btn sm" type="button" id="md-close">关闭</button></div>
        <div class="tabs" role="tablist">${tabs}</div>
        <div class="card-body">${body}</div>
      </div>`;
      host.querySelectorAll('[data-tab]').forEach((b) => {
        b.onclick = () => { setParam('tab', b.dataset.tab, { reload: false }); };
      });
      const close = $('#md-close');
      if (close) close.onclick = () => {
        const p = currentParams(); p.delete('id'); p.delete('tab'); writeHash(p, { navigate: false });
        host.classList.add('hidden'); host.innerHTML = ''; host.dataset.uid = '';
      };
      const renew = $('#md-renew');
      if (renew) renew.onclick = () => memberRenew(id);
      const groupGo = $('#md-group-go');
      if (groupGo) groupGo.onclick = () => memberGroup(id);
      const retry = $('#md-retry');
      if (retry) retry.onclick = () => retryRemote(id);
      host.querySelectorAll('[data-dev]').forEach((b) => {
        b.onclick = async () => {
          const blocked = b.dataset.block === '1';
          const path = blocked ? 'block' : 'unblock';
          await api(`/api/members/${encodeURIComponent(id)}/devices/${encodeURIComponent(b.dataset.dev)}/${path}`, { method: 'POST' });
          fillDetail(id, 'devices');
        };
      });
    } catch (e) {
      host.innerHTML = `<div class="card"><div class="card-body">详情失败：${esc(e.message)}</div></div>`;
    }
  }

  async function memberRenew(id) {
    const days = Number(($('#md-days') || {}).value || 30);
    if (!days) return toast('请填写续期天数', 1);
    const preview = await api(`/api/members/${encodeURIComponent(id)}/renew-preview?days=${days}`);
    if (!preview.allowed) return toast((preview.warnings || ['不可续期'])[0], 1);
    if (!confirm(`将从 ${fmtExpiry(preview.current_expires_at_effective)} 续到 ${fmtExpiry(preview.new_expires_at)}。${preview.writes_override ? '写入个人覆盖层。' : ''}`)) return;
    const r = await api(`/api/members/${encodeURIComponent(id)}/renew`, {
      method: 'POST', body: JSON.stringify({ days }),
    });
    toastResult(r, '已续期');
    await refreshNow();
    fillDetail(id, currentParams().get('tab') || 'overview');
  }

  async function memberGroup(id) {
    const gid = ($('#md-group') || {}).value;
    const policy = ($('#md-policy') || {}).value || 'keep';
    if (!gid) return;
    const preview = await api(`/api/members/${encodeURIComponent(id)}/group-preview?group_id=${encodeURIComponent(gid)}`);
    if (preview.decision_required && policy === 'keep') {
      if (!confirm('永久/无到期账号切到计时组。确定保留不限期？选“套用目标组天数”才会开始计时。')) return;
    } else if ((preview.warnings || []).length) {
      if (!confirm((preview.warnings || []).join('\n') + '\n确定换组？')) return;
    }
    const r = await api(`/api/members/${encodeURIComponent(id)}/group`, {
      method: 'POST', body: JSON.stringify({ group_id: gid, expiry_policy: policy }),
    });
    toastResult(r, '已换组');
    await refreshNow();
    fillDetail(id, currentParams().get('tab') || 'overview');
  }

  async function retryRemote(id) {
    const r = await api(`/api/members/${encodeURIComponent(id)}/retry-remote`, { method: 'POST' });
    toastResult(r, '已重试');
    await refreshNow();
  }

  async function confirmDelete(id, name) {
    const preview = await api(`/api/members/${encodeURIComponent(id)}/delete-preview`);
    const available = preview.available_cascade || [];
    let cascade = false;
    if (available.length) {
      cascade = confirm(`删除 ${name || id}？\n默认只删本人。\n确定要连带邀请人（${available.map((c) => c.username).join('、')}）请再点一次确认。\n先取消则只删本人。`);
      if (cascade) {
        const casc = await api(`/api/members/${encodeURIComponent(id)}/delete-preview?cascade=true`);
        const names = (casc.objects || []).map((o) => o.username || o.emby_user_id).join('、');
        if (!confirm(`将删除：${names}\n提交不会扩大到预览之外的账号。`)) return;
        const r = await api(`/api/members/${encodeURIComponent(id)}?cascade=true`, {
          method: 'DELETE',
          body: JSON.stringify({
            cascade: true,
            confirm_ids: (casc.objects || []).map((o) => o.emby_user_id),
          }),
        });
        if (!toastResult(r, '已删除')) {
          (r.emby_failed || []).forEach((f) => toast(`${f.user_id}: ${f.error}`, 1));
        }
        ms.selected.delete(id);
        return refreshNow();
      }
    } else if (!confirm(`删除 ${name || id}？只删本人，不可恢复。`)) {
      return;
    }
    const r = await api(`/api/members/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (!toastResult(r, '已删除')) {
      (r.emby_failed || []).forEach((f) => toast(`${f.user_id}: ${f.error}`, 1));
    }
    ms.selected.delete(id);
    await refreshNow();
  }

  async function bulk(action) {
    const ids = [...ms.selected];
    if (!ids.length) return toast('没有选中的用户', 1);
    if (action === 'renew') {
      const days = Number(prompt('续期天数', '30') || '0');
      if (!days) return;
      const r = await api('/api/members/bulk', {
        method: 'POST', body: JSON.stringify({ action: 'renew', user_ids: ids, days }),
      });
      if (r.ok_flag === false || (r.remote_failed || []).length) toast('部分失败', 1);
      else toast(`已续期 ${r.ok || 0} 人`);
    } else {
      const r = await api('/api/members/bulk', {
        method: 'POST', body: JSON.stringify({ action, user_ids: ids }),
      });
      if (r.ok_flag === false || (r.remote_failed || []).length) toast('部分失败', 1);
      else toast(`已更新 ${r.ok || 0} 人`);
    }
    await refreshNow();
  }

  async function enrol(id, name) {
    const groups = ms.groups || [];
    const gid = (groups.find((g) => g.is_default) || groups[0] || {}).id;
    const r = await api(`/api/members/${encodeURIComponent(id)}`, {
      method: 'PUT', body: JSON.stringify({ username: name, group_id: gid }),
    });
    toastResult(r, '已纳入');
    await refreshNow();
  }

  async function showEnforcement() {
    const r = await api('/api/enforcement/preview');
    openModal('策略预览', `
      <div class="help">预览不会写入。管理员与未纳管账号会被跳过。</div>
      ${tableCard('将变更', `${(r.changes || []).length} 个`, ['用户', '状态', '字段'],
        (r.changes || []).map((c) => `<tr><td>${esc(c.username)}</td><td>${esc(c.state)}</td>
          <td>${esc(Object.keys(c.changes || {}).join(', ') || '-')}</td></tr>`).join(''))}
      ${tableCard('已跳过', `${(r.skipped || []).length} 个`, ['用户', '原因'],
        (r.skipped || []).map((s) => `<tr><td>${esc(s.username)}</td><td>${esc(s.reason)}</td></tr>`).join(''))}
    `, { wide: true });
  }

  function stopFallbackLive() {
    if (ms.liveSrc) { ms.liveSrc.close(); ms.liveSrc = null; }
  }

  function startFallbackLive() {
    if (typeof registerLiveUpdater === 'function') return;
    if (ms.liveSrc) return;
    const src = new EventSource('/api/stream?topics=members');
    src.addEventListener('members', () => {
      if (typeof state !== 'undefined' && String(state.page || '').split('?')[0] !== 'members') return;
      if (!$('#members-page')) return;
      if (typeof isEditing === 'function' && isEditing()) return;
      loadMembers(membersContext(true));
    });
    ms.liveSrc = src;
  }

  async function onMembersLive(topic, payload, context) {
    if (context && typeof context.isCurrent === 'function' && !context.isCurrent()) return;
    if (typeof isEditing === 'function' && isEditing()) return;
    if (!$('#members-page')) return;
    void topic;
    void payload;
    await loadMembers(context || membersContext(true));
  }

  PAGES.members = async (context) => {
    context = context || membersContext(false);
    if (typeof state !== 'undefined') state.page = 'members';
    if (!context.live) ms.groups = [];
    startFallbackLive();
    await loadMembers(context);
  };

  if (typeof registerLiveUpdater === 'function' && !ms.liveBound) {
    registerLiveUpdater('members', ['members'], onMembersLive);
    ms.liveBound = true;
  }

  window.addEventListener('hashchange', () => {
    const parsed = parseMembersHash();
    if (parsed.page !== 'members') {
      stopFallbackLive();
      return;
    }
    if (hasLiveShell()) return;
    if (ms.writingHash) return;
    if ($('#members-page')) loadMembers(membersContext(false));
    else PAGES.members(membersContext(false));
  });

  window.parseMembersHash = parseMembersHash;
  window.membersQueryFromParams = membersQueryFromParams;
  window.membersToastFromResult = toastResult;
  window.membersConfirmIdsFromPreview = (preview) =>
    (preview.objects || []).map((o) => o.emby_user_id);

  if (!hasLiveShell()) {
    const raw = (location.hash || '').replace(/^#\/?/, '');
    if (raw.startsWith('members?')) PAGES[raw] = (ctx) => PAGES.members(ctx);
  }

  if (typeof bootPanel === 'function') bootPanel();

  if (!hasLiveShell()) {
    const parsed = parseMembersHash();
    if (parsed.page === 'members' && !$('#members-page')) {
      if (typeof state !== 'undefined') state.page = 'members';
      PAGES.members(membersContext(false));
    }
  }
})();
