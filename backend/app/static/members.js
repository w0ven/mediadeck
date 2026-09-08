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
    detailVersion: 0,
    detail: null,
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
      role: 'role', register_via: 'register_via', inviter_id: 'inviter_id',
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
    if (hasLiveShell()) state.route = route;
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

  function measuredBytes(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '未测';
    const units = ['B','KiB','MiB','GiB','TiB'];
    let number = Number(value), index = 0;
    while (number >= 1024 && index < units.length - 1) { number /= 1024; index++; }
    return (index ? number.toFixed(1) : String(number)) + ' ' + units[index];
  }

  function usageCell(m) {
    const measuredMode = m.quota_source === 'measured'
      || Object.prototype.hasOwnProperty.call(m, 'measured_used_bytes');
    const sample = m.metering || {};
    const used = measuredMode ? m.measured_used_bytes : m.traffic_used_bytes;
    const quota = m.traffic_quota_bytes ? measuredBytes(m.traffic_quota_bytes) : '不限';
    const label = measuredMode ? '本月流量' : '估算配额（未切换实测）';
    const coverage = sample.coverage || {};
    const usageLabel = sample.measurement_status === 'no_usage_records'
      ? '本月尚无实测记录' : measuredBytes(used);
    const missingNodes = (coverage.nodes || []).filter(n => !n.ok).map(n => n.name).join('、');
    return `<div class="s"><b>${label}</b> ${esc(usageLabel)} / ${esc(quota)}
      ${!measuredMode && m.metering ? `<div class="muted">实测监测 ${esc(measuredBytes(sample.measured_used_bytes))}（未用于限额）</div>` : ''}
      ${coverage.degraded ? `<div class="tag warn">采集不完整${missingNodes ? '：' + esc(missingNodes) : ''} · 待恢复核实</div>` : ''}</div>`;
  }

  function accountCell(m) {
    const tg = m.tg_username ? '@' + m.tg_username : (m.tg_user_id ? '已绑定' : '');
    return `<button class="linkish" type="button" data-act="open" data-id="${esc(m.emby_user_id)}">${esc(m.username || m.emby_user_id)}</button>
      <div class="s muted">${esc(tg || '未绑定 TG')}</div>`;
  }

  function expiryCell(m) {
    const ts = (m.expires_at_effective !== undefined ? m.expires_at_effective : m.expires_at);
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
        <button class="btn sm danger" type="button" data-act="delete" data-id="${esc(id)}" data-name="${esc(m.username)}">删除本人</button>
        ${m.inviter_id ? `<button class="btn sm danger" type="button" data-act="delete-cascade" data-id="${esc(id)}" data-name="${esc(m.username)}">连带邀请人…</button>` : ''}
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
          ${[...new Set([Number(size), 25, 50, 100])].sort((a,b) => a-b).map((n) => `<option value="${n}" ${n === Number(size) ? 'selected' : ''}>${n}</option>`).join('')}
        </select>
      </label>
    </div>`;
  }

  function filterBar(params, groups) {
    const pick = (id, key, label, options, fallback = '') => `<label>${label} <select id="${id}" aria-label="${label}">${options.map(([value,text]) => `<option value="${value}" ${(params.get(key) || fallback) === value ? 'selected' : ''}>${text}</option>`).join('')}</select></label>`;
    const labels = {active:'正常',expired:'已过期',exhausted:'额度用尽',suspended:'手动停用',pending:'待开通',present:'存在',missing:'缺失',unknown:'未知',in_sync:'已同步',drift:'策略漂移',failed:'失败',never_applied:'从未下发',emby_missing:'账号缺失'};
    const gopts = groups.map((g) =>
      `<option value="${esc(g.id)}" ${params.get('group_id') === g.id ? 'selected' : ''}>${esc(g.name)}</option>`).join('');
    return `<div class="toolbar members-filters" id="members-filters" data-live-preserve>
      <label>搜索 <input id="m-q" type="search" value="${esc(params.get('q') || '')}" placeholder="账号 / 备注 / 联系方式" aria-label="搜索用户"></label>
      <label>状态 <select id="m-status" aria-label="权益状态">
        <option value="">全部</option>
        ${['active', 'expired', 'exhausted', 'suspended', 'pending'].map((s) =>
          `<option value="${s}" ${params.get('status') === s ? 'selected' : ''}>${labels[s] || s}</option>`).join('')}
      </select></label>
      <label>用户组 <select id="m-group" aria-label="用户组"><option value="">全部</option>${gopts}</select></label>
      <label>Emby <select id="m-emby" aria-label="Emby 状态">
        <option value="">全部</option>
        ${['present', 'missing', 'unknown'].map((s) =>
          `<option value="${s}" ${params.get('emby_status') === s ? 'selected' : ''}>${labels[s] || s}</option>`).join('')}
      </select></label>
      <label>同步 <select id="m-sync" aria-label="同步状态">
        <option value="">全部</option>
        ${['in_sync', 'drift', 'failed', 'never_applied', 'emby_missing'].map((s) =>
          `<option value="${s}" ${params.get('sync_status') === s ? 'selected' : ''}>${labels[s] || s}</option>`).join('')}
      </select></label>
      ${pick('m-sort','sort','排序',[['username','账号'],['group','用户组'],['expires','有效期'],['traffic','本月流量'],['last_seen','最近活跃']], 'username')}
      ${pick('m-order','order','顺序',[['asc','升序'],['desc','降序']], 'asc')}
      <details><summary>更多筛选</summary><div class="toolbar">
        ${pick('m-tg','tg','TG绑定',[['','全部'],['bound','已绑定'],['unbound','未绑定']])}
        ${pick('m-expiring','expiring','到期',[['','全部'],['soon','7天内'],['gone','已过期']])}
        ${pick('m-role','role','角色',[['','全部'],['admin','管理员'],['uploader','上片员']])}
        ${pick('m-via','register_via','注册渠道',[['','全部'],['admin','管理员授权'],['invite','邀请'],['redeem','卡密'],['legacy','历史导入']])}
      </div></details>
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
        <button class="btn sm" type="button" data-act="bulk" data-bulk="activate">启用</button>
        <button class="btn sm" type="button" data-act="bulk" data-bulk="reset-traffic">重置用量</button>
        <button class="btn sm" type="button" data-act="clear-selection">取消选择</button>
        <button class="btn sm" type="button" data-act="enforce">策略预览</button>
        <button class="btn sm" type="button" data-act="metering">实测计量与接管预览</button>
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
      ['m-sync', 'sync_status'], ['m-sort','sort'], ['m-order','order'], ['m-tg','tg'],
      ['m-expiring','expiring'], ['m-role','role'], ['m-via','register_via']].forEach(([id, key]) => {
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
    root.onclick = async (e) => {
      const btn = e.target.closest('[data-act]');
      if (!btn || btn.disabled) return;
      const act = btn.dataset.act;
      const id = btn.dataset.id;
      try {
        if (act === 'open') await openDetail(id);
        if (act === 'delete') await confirmDelete(id, btn.dataset.name, false);
        if (act === 'delete-cascade') await confirmDelete(id, btn.dataset.name, true);
        if (act === 'retry') await retryRemote(id);
        if (act === 'page') setParam('page', btn.dataset.page);
        if (act === 'bulk') await bulk(btn.dataset.bulk);
        if (act === 'clear-selection') { ms.selected.clear(); patchSelection(); }
        if (act === 'enforce') await showEnforcement();
        if (act === 'metering') await showMetering();
        if (act === 'enrol') await enrol(id, btn.dataset.name);
        if (act === 'invitees') setParam('inviter_id', id);
      } catch (error) { toast('操作失败: ' + error.message, 1); }
    };
    root.onchange = (e) => {
      const box = e.target.closest('.m-pick');
      if (!box) return;
      if (box.checked) ms.selected.add(box.dataset.id);
      else ms.selected.delete(box.dataset.id);
      patchSelection();
    };
    root.onkeydown = (e) => {
      if (e.target.closest('button,input,select,textarea,a')) return;
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
    const version = ++ms.detailVersion;
    const current = () => version === ms.detailVersion && host.isConnected && host.dataset.uid === id;
    host.classList.remove('hidden');
    host.dataset.uid = id;
    host.innerHTML = '<div class="card"><div class="card-body">加载详情…</div></div>';
    try {
      const d = await api(`/api/members/${encodeURIComponent(id)}?days=30`);
      if (!current()) return;
      ms.detail = d;
      const m = d.member || {};
      const libs = tab === 'entitlements' ? await api('/api/emby/libraries').catch(() => []) : [];
      if (!current()) return;
      const tabs = TABS.map(([k, label]) =>
        `<button class="tab ${k === tab ? 'active' : ''}" type="button" data-tab="${k}">${esc(label)}</button>`).join('');
      let body = '';
      if (tab === 'overview') {
        const gopts = (ms.groups || []).map((g) =>
          `<option value="${esc(g.id)}" ${g.id === m.group_id ? 'selected' : ''}>${esc(g.name)}</option>`).join('');
        body = `<dl class="member-kv">
          <dt>权益</dt><dd>${entitlementTag(m)} ${esc(m.state_reason || '')}</dd>
          <dt>Emby</dt><dd>${embySyncCell(m)}</dd>
          <dt>到期</dt><dd>${esc(fmtExpiry((m.expires_at_effective !== undefined ? m.expires_at_effective : m.expires_at)))}</dd>
          <dt>配额用量</dt><dd>${usageCell(m)}</dd>
          ${m.metering ? `<dt>实测周期</dt><dd>${esc(m.metering.period || '未知')}（UTC自然月） · 最近上报 ${esc(m.metering.as_of ? fmtAgeTs(m.metering.as_of) : '未知')}</dd>` : ''}
          <dt>近24小时观看</dt><dd>${watchWindowLabel(d.watch, '24h')}</dd>
          <dt>近30天观看</dt><dd>${watchWindowLabel(d.watch, '30d')}</dd>
          <dt>累计观看</dt><dd>${d.watch ? esc(fmtWatchSeconds(d.watch.recorded_seconds)) : '暂无统计'}</dd>
        </dl>
        <div class="toolbar" id="md-actions">
          <label>续期 <input id="md-days" type="number" min="1" value="30" style="width:72px"> 天
            <button class="btn sm" type="button" id="md-renew">续期</button></label>
          <label>换组 <select id="md-group">${gopts}</select>
            <select id="md-policy">
              <option value="keep">保留期限（不计时组改为不限期）</option>
              <option value="apply_group">套用目标组天数</option>
              <option value="clear">改为不限期</option>
            </select>
            <button class="btn sm" type="button" id="md-group-go">换组</button></label>
          <button class="btn sm" type="button" id="md-retry">重试远端</button>
          <button class="btn sm" type="button" data-member-action="status">${m.state === 'suspended' ? '解除手动停用' : '停用账号'}</button>
          <button class="btn sm" type="button" data-member-action="password">重置密码</button>
          <button class="btn sm" type="button" data-member-action="kick">结束当前播放</button>
          <button class="btn sm" type="button" data-member-action="reset-traffic">重置本月用量</button>
          ${m.tg_user_id ? '<button class="btn sm" type="button" data-member-action="telegram/unbind">解除TG绑定</button>' : ''}
        </div>
        <p class="help">流量与 Bot 使用同一实测账本；观看按实际采样区间累计，不补算暂停或停机时长。</p>`;
      } else if (tab === 'entitlements') {
        body = `<p class="help">组 ${esc(m.group_name)}；个人权限覆盖优先，但不能开启用户组未启用的计费维度。不计时组不限期，不计流量组不限流量。修改后点击保存才生效。</p>
          <div class="toolbar" id="md-roles">
            ${[['admin','管理员（可登录面板）'],['uploader','上片员']].map(([role,label]) => `<label><input type="checkbox" class="md-role" value="${role}" ${(m.roles || []).includes(role) ? 'checked' : ''}> ${label}</label>`).join('')}
            <button class="btn sm" type="button" id="md-roles-save">保存角色</button>
          </div><div id="md-overrides">${overrideEditor(m, libs)}</div>`;
      } else if (tab === 'devices') {
        const devices = d.devices || [];
        const plays = d.plays || d.recent_plays || [];
        body = devices.length
          ? `<table><thead><tr><th>设备</th><th>客户端</th><th></th></tr></thead><tbody>${devices.map((x) => `<tr>
              <td>${esc(x.device_name || x.device_id)}</td><td>${esc(x.client || '')}</td>
              <td><button class="btn sm" type="button" data-dev="${esc(x.device_id)}" data-block="${x.blocked ? '0' : '1'}">${x.blocked ? '解禁' : '封锁'}</button>
              <button class="btn sm" type="button" data-forget-device="${esc(x.device_id)}">移除设备记录</button></td>
            </tr>`).join('')}</tbody></table>`
          : '<div class="empty">无设备记录</div>';
        body += `<h4>最近播放</h4>` + (plays.length
          ? `<ul>${plays.map((p) => `<li>${esc(p.item_name || p.Name || p.item || '—')}</li>`).join('')}</ul>`
          : '<div class="empty">暂无播放</div>');
      } else if (tab === 'invites') {
        body = `<p>积分 ${esc(d.points || 0)} · 邀请名额 ${esc(m.invite_quota || 0)} · 下级 ${esc(m.invitee_count || 0)}</p>
          <p>邀请人：${esc(m.inviter_name || '—')} · 注册渠道：${esc(m.register_via || 'legacy')} <button class="btn sm" data-act="invitees" data-id="${esc(id)}">查看其邀请用户</button></p>
          <div class="toolbar"><label>调整积分 <input id="md-points-delta" type="number" step="1" value="0"></label><label>原因 <input id="md-points-reason" maxlength="100"></label><button class="btn sm" id="md-points-save">提交调整</button></div>
          <details><summary>积分流水</summary>${(d.points_ledger || []).map((entry) => `<p>${esc(entry.delta)} · ${esc(entry.reason || entry.action || '')}</p>`).join('') || '<p>暂无流水</p>'}</details>
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
        ms.detailVersion++; ms.detail = null;
        host.classList.add('hidden'); host.innerHTML = ''; host.dataset.uid = '';
      };
      bindMemberActions(host, id, m, tab);
      const renew = $('#md-renew');
      if (renew) renew.onclick = () => runMemberAction(renew, () => memberRenew(id));
      const groupGo = $('#md-group-go');
      if (groupGo) groupGo.onclick = () => runMemberAction(groupGo, () => memberGroup(id));
      const retry = $('#md-retry');
      if (retry) retry.onclick = () => runMemberAction(retry, () => retryRemote(id));
      host.querySelectorAll('[data-dev]').forEach((b) => {
        b.onclick = () => runMemberAction(b, async () => {
          const blocked = b.dataset.block === '1';
          if (!confirm(`${blocked ? '封锁' : '解封'}这个设备？`)) return;
          const path = blocked ? 'block' : 'unblock';
          const r = await api(`/api/members/${encodeURIComponent(id)}/devices/${encodeURIComponent(b.dataset.dev)}/${path}`, { method: 'POST' });
          assertRemoteResult(r);
          fillDetail(id, 'devices');
        });
      });
    } catch (e) {
      if (!current()) return;
      host.innerHTML = `<div class="card"><div class="card-body">详情失败：${esc(e.message)}</div></div>`;
    }
  }

  function assertRemoteResult(result) {
    if (!result || result.ok === false || result.remote_ok === false || result.local_ok === false) {
      throw new Error((result && (result.error || result.errors?.[0]?.error)) || '远端操作未完成');
    }
    return result;
  }

  async function runMemberAction(button, action) {
    if (button?.disabled) return;
    if (button) button.disabled = true;
    try { await action(); } catch (error) { toast('操作失败: ' + error.message, 1); }
    finally { if (button?.isConnected) button.disabled = false; }
  }

  function edgeHistory(edge) {
    if (!edge) return '';
    const nodes = edge.by_node || [];
    const days = edge.by_day || [];
    return `<details><summary>直链节点与每日用量</summary>
      ${nodes.length ? `<table><thead><tr><th>节点</th><th>发送字节</th><th>请求</th></tr></thead><tbody>${nodes.map((n) => `<tr><td>${esc(n.node)}</td><td>${fmtBytes(n.bytes)}</td><td>${esc(n.requests)}</td></tr>`).join('')}</tbody></table>` : '<p>暂无节点账本</p>'}
      ${days.length ? `<table><thead><tr><th>日期</th><th>发送字节</th></tr></thead><tbody>${days.map((d) => `<tr><td>${esc(d.day)}</td><td>${fmtBytes(d.bytes)}</td></tr>`).join('')}</tbody></table>` : ''}</details>`;
  }

  function resetOverrideInput(key) {
    const ids = {max_streams:'ov-streams',bandwidth_limit_kbps:'ov-bandwidth',max_devices:'ov-devices',
      allow_transcode:'ov-transcode',allow_download:'ov-download',extra_traffic_bytes:'ov-extra'};
    if (ids[key] && document.getElementById(ids[key])) document.getElementById(ids[key]).value = '';
    if (key === 'libraries') {
      $('#ov-libmode').value = 'inherit';
      document.querySelectorAll('.ov-lib').forEach((box) => { box.checked = false; });
    }
    if (key === 'expires_at_override') {
      $('#ov-exp-mode').value = 'inherit'; $('#ov-exp').value = '';
    }
  }

  function bindMemberActions(host, id, member, tab) {
    const endpoint = `/api/members/${encodeURIComponent(id)}`;
    host.querySelectorAll('[data-member-action]').forEach((button) => {
      button.onclick = () => runMemberAction(button, async () => {
        const action = button.dataset.memberAction;
        let body = {};
        if (action === 'password') {
          const value = prompt('新密码（至少6位；留空随机生成）', '');
          if (value === null) return;
          if (value && value.length < 6) throw new Error('密码至少6位');
          body = value ? {password:value} : {};
        } else {
          const names = {status:member.state === 'suspended' ? '解除手动停用' : '停用账号',
            kick:'结束当前所有播放','reset-traffic':'重置本月已用额度（保留历史账本）',
            'telegram/unbind':'解除Telegram绑定'};
          if (!confirm(`确认${names[action]}？`)) return;
          if (action === 'status') body.status = member.state === 'suspended' ? 'active' : 'suspended';
        }
        const result = assertRemoteResult(await api(endpoint + '/' + action, {
          method:'POST', body:JSON.stringify(body),
        }));
        if (action === 'password' && result.password) {
          openModal('新密码（仅本次展示）', `<div class="card-body"><label>新密码 <input type="text" readonly autocomplete="off" value="${esc(result.password)}"></label><p>请妥善保存，关闭后不再显示。</p></div>`);
        } else toast(action === 'kick' ? `已结束 ${result.stopped || 0} 路播放` : '操作已完成');
        await refreshNow();
        await fillDetail(id, tab);
      });
    });
    host.querySelectorAll('[data-forget-device]').forEach((button) => {
      button.onclick = () => runMemberAction(button, async () => {
        if (!confirm('移除这个设备的面板记录？不会删除其它设备。')) return;
        assertRemoteResult(await api(endpoint + '/devices/' + encodeURIComponent(button.dataset.forgetDevice), {method:'DELETE'}));
        await fillDetail(id, 'devices');
      });
    });
    const roles = $('#md-roles-save');
    if (roles) roles.onclick = () => runMemberAction(roles, async () => {
      const selected = [...host.querySelectorAll('.md-role:checked')].map((input) => input.value);
      if (!confirm(`确认修改角色为 ${selected.join('、') || '普通成员'}？管理员角色允许登录管理面板。`)) return;
      assertRemoteResult(await api(endpoint + '/roles', {method:'POST',body:JSON.stringify({roles:selected})}));
      toast('角色已保存'); await refreshNow(); await fillDetail(id, 'entitlements');
    });
    const save = $('#ov-save');
    if (save) save.onclick = () => runMemberAction(save, async () => {
      if (![...host.querySelectorAll('#md-overrides input')].every((input) => input.reportValidity())) return;
      const overrides = collectOverridesFromForm(member.overrides || {});
      if (!confirm('保存个人权限覆盖？限速变化可能结束当前播放以重新生效。')) return;
      const result = await api(endpoint + '/overrides', {method:'PUT',body:JSON.stringify(overrides)});
      if (!toastResult(result, '权限覆盖已保存')) return;
      await refreshNow(); await fillDetail(id, 'entitlements');
    });
    const clear = $('#ov-clear');
    if (clear) clear.onclick = () => runMemberAction(clear, async () => {
      if (!confirm('清除全部个人覆盖并继承用户组？')) return;
      const result = await api(endpoint + '/overrides', {method:'PUT',body:'{}'});
      if (!toastResult(result, '个人覆盖已清除')) return;
      await refreshNow(); await fillDetail(id, 'entitlements');
    });
    const points = $('#md-points-save');
    if (points) points.onclick = () => runMemberAction(points, async () => {
      const delta = Number($('#md-points-delta').value);
      const reason = $('#md-points-reason').value.trim();
      if (!Number.isInteger(delta) || delta === 0 || !reason) throw new Error('请填写非零整数积分及调整原因');
      if (!confirm(`确认调整积分 ${delta > 0 ? '+' : ''}${delta}？原因：${reason}`)) return;
      assertRemoteResult(await api(`/api/points/${encodeURIComponent(id)}/adjust`, {method:'POST',body:JSON.stringify({delta,reason})}));
      toast('积分已调整'); await fillDetail(id, 'invites');
    });
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
    const selected = (preview.policies || {})[policy] || {};
    const lines = [
      `用户组：${(preview.from_group || {}).name || '未分组'} → ${(preview.to_group || {}).name || gid}`,
      `原有效期：${fmtExpiry(preview.current_expires_at_effective)}`,
      `新有效期：${fmtExpiry(selected.expires_at)}`,
      ...(preview.warnings || []),
      '其他个人权限与历史用量保留。确认换组？'
    ];
    if (!confirm(lines.join('\n'))) return;
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

  async function confirmDelete(id, name, cascade = false) {
    const preview = await api(`/api/members/${encodeURIComponent(id)}/delete-preview?cascade=${cascade}`);
    const objects = preview.objects || [];
    if (!objects.length || (cascade && objects.length < 2)) return toast('没有可确认的删除对象，请刷新后重试', 1);
    const names = objects.map((o) => o.username || o.emby_user_id).join('、');
    const message = cascade
      ? `连带删除邀请人：${names}。\n将删除所列 Emby 账号及面板记录，不可恢复。`
      : `仅删除 ${name || id}，保留邀请人。\n将删除该 Emby 账号及面板记录，不可恢复。`;
    if (!confirm(message)) return;
    const r = await api(`/api/members/${encodeURIComponent(id)}?cascade=${cascade}`, {
      method: 'DELETE', body: JSON.stringify({cascade, confirm_ids: objects.map((o) => o.emby_user_id)}),
    });
    const ok = toastResult(r, '已删除');
    if (!ok) (r.emby_failed || []).forEach((f) => toast(`${f.user_id}: ${f.error}`, 1));
    if (ok) objects.forEach((o) => ms.selected.delete(o.emby_user_id));
    else (r.removed || []).forEach((uid) => ms.selected.delete(uid));
    await refreshNow();
  }

  async function bulk(action) {
    const ids = [...ms.selected];
    if (!ids.length) return toast('没有选中的用户', 1);
    if (!confirm(`对已明确勾选的 ${ids.length} 人执行 ${({renew:'续期',suspend:'停用',activate:'启用','reset-traffic':'重置用量'})[action]}？`)) return;
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

  async function showMetering() {
    const status = await api('/api/metering');
    const config = status.config || {};
    const totals = status.totals || {};
    const nodes = (totals.coverage || {}).nodes || [];
    const nodeTotals = new Map((totals.by_node || []).map((node) => [node.node, node]));
    const incomplete = !nodes.length || nodes.some((node) => !node.ok);
    const users = Object.entries(totals.by_user || {});
    openModal('实测计量与接管预览', `<div class="card-body">
      <p><b>${config.cutover ? '实测配额已启用' : '仅监测，尚未启用实测限额'}</b> · 周期 ${esc(totals.period || '未知')}（UTC自然月）</p>
      <p class="help">计量只汇总已注册播放连接的内核出站IP字节，包含协议头及内核计入的重传；不是文件大小或物理网卡逐帧统计。旧会话估算和旧直链日志不会相加收费。</p>
      ${incomplete ? '<p class="help danger-text">有节点尚未报告或已过期，计量覆盖不完整。未知数据不会当作零；同步故障优先保播放，已知阻断不应自动解除。</p>' : ''}
      <table><thead><tr><th>节点</th><th>上报状态</th><th>最近报告</th><th>本月记录</th></tr></thead><tbody>${nodes.map((node) => `<tr><td>${esc(node.name)}</td><td>${node.ok ? '正常' : '未知/异常'}</td><td>${esc(node.as_of ? fmtAgeTs(node.as_of) : '从未上报')}</td><td>${measuredBytes((nodeTotals.get(node.name) || {}).bytes)}</td></tr>`).join('') || '<tr><td colspan="4">暂无节点报告</td></tr>'}</tbody></table>
      <p>未归属/无法确定归期：${measuredBytes(totals.unattributed_bytes)}；上报间隔目标 ${esc(config.report_interval_seconds || 15)} 秒，实际以节点报告时间为准。</p>
      <details><summary>核对本月用户实测记录（${users.length} 人）</summary><p class="help">此处是保留的计量记录；重置后的配额用量请同时核对用户详情。</p><div id="meter-users"></div><div class="toolbar"><button class="btn sm" id="meter-prev">上一页</button><span id="meter-page"></span><button class="btn sm" id="meter-next">下一页</button></div></details>
      <p class="help">启用后，计流量用户达到额度将被中断并拒绝后续播放；不会替你迁移 embyboss 的用户权益。请先完成节点部署和旧用户资料核对。</p>
      ${config.cutover ? '' : '<label><input type="checkbox" id="meter-baseline"> 我已核对计量覆盖和各用户配额余额，确认以当前实测账本接管限额</label>'}
      <div class="toolbar"><button class="btn danger" id="meter-cutover">${config.cutover ? '停用实测限额' : '确认启用实测限额'}</button></div>
    </div>`, {wide:true});
    let page = 0;
    const draw = () => {
      const pages = Math.max(1, Math.ceil(users.length / 50));
      $('#meter-users').innerHTML = `<table><thead><tr><th>用户ID</th><th>实测记录</th></tr></thead><tbody>${users.slice(page*50, page*50+50).map(([id,bytes]) => `<tr><td>${esc(id)}</td><td>${measuredBytes(bytes)}</td></tr>`).join('')}</tbody></table>`;
      $('#meter-page').textContent = `${page+1} / ${pages}`;
      $('#meter-prev').disabled = page === 0; $('#meter-next').disabled = page+1 >= pages;
    };
    $('#meter-prev').onclick = () => { page--; draw(); };
    $('#meter-next').onclick = () => { page++; draw(); };
    draw();
    const button = $('#meter-cutover');
    button.onclick = () => runMemberAction(button, async () => {
      const enable = !config.cutover;
      if (enable && !$('#meter-baseline').checked) throw new Error('请先明确确认计量基线与配额余额');
      if (!confirm(enable
        ? `确认启用实测配额并允许超额中断播放？${incomplete ? '当前存在未就绪节点，覆盖不完整。' : ''}`
        : '确认停用实测限额并切回原计量来源？不会恢复过期或手动停用用户。')) return;
      assertRemoteResult(await api('/api/metering/cutover', {method:'POST',body:JSON.stringify({cutover:enable,baseline_confirmed:enable || !!config.baseline_confirmed})}));
      toast(enable ? '已启用实测限额' : '已停用实测限额');
      closeModal(); await refreshNow();
    });
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

  window.enforcementPreview = showEnforcement;
  window.clearOverrideField = resetOverrideInput;
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
