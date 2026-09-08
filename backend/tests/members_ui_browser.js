/* Real Chromium, real panel scripts, mocked API/SSE. No production traffic. */
(async () => {
  const report = document.getElementById('report');
  let checks = 0;
  const assert = (value, label) => { if (!value) throw new Error(label); checks++; };
  const tick = (ms = 40) => new Promise((resolve) => setTimeout(resolve, ms));
  async function waitFor(pred, label, tries = 80) {
    for (let i = 0; i < tries; i++) {
      if (pred()) return;
      await tick(40);
    }
    throw new Error(label);
  }

  const requests = [];
  const toasts = [];
  const confirms = [];
  const confirmAnswers = [];
  const sources = [];
  let lastDelete = null;
  let lastRenew = null;
  let lastGroup = null;
  let lastRetry = null;
  let meterConfig = {cutover:false, baseline_confirmed:false, report_interval_seconds:15};

  class FakeSource {
    constructor(url) {
      this.url = url;
      this.handlers = {};
      this.closed = false;
      sources.push(this);
    }
    addEventListener(topic, handler) { this.handlers[topic] = handler; }
    emit(topic, data) {
      if (this.handlers[topic]) this.handlers[topic]({ data: JSON.stringify(data) });
    }
    close() { this.closed = true; }
  }
  window.EventSource = FakeSource;

  const now = Math.floor(Date.now() / 1000);
  const GROUPS = [
    { id: 'standard', name: '标准月卡', billing_mode: 'both', duration_days: 30, is_default: true },
    { id: 'vip', name: 'VIP季卡', billing_mode: 'both', duration_days: 90 },
    { id: 'perm', name: '永久', billing_mode: 'none', duration_days: 0 },
  ];

  const mk = (id, username, extra = {}) => ({
    emby_user_id: id,
    username,
    group_id: extra.group_id || 'standard',
    group_name: extra.group_name || '标准月卡',
    entitlement_state: extra.entitlement_state || extra.state || 'active',
    state: extra.state || extra.entitlement_state || 'active',
    emby_status: extra.emby_status || 'present',
    emby_disabled: !!extra.emby_disabled,
    emby_is_admin: !!extra.emby_is_admin,
    sync_status: extra.sync_status || 'in_sync',
    expires_at: extra.expires_at === undefined ? now + 20 * 86400 : extra.expires_at,
    expires_at_effective: extra.expires_at_effective === undefined
      ? (extra.expires_at === undefined ? now + 20 * 86400 : extra.expires_at)
      : extra.expires_at_effective,
    traffic_used_bytes: extra.traffic_used_bytes || 0,
    traffic_quota_bytes: extra.traffic_quota_bytes || 0,
    retryable: !!extra.retryable,
    last_activity: extra.last_activity || null,
    last_seen_at: extra.last_seen_at || now - 3600,
    tg_username: extra.tg_username === undefined ? username : extra.tg_username,
    edge: extra.edge || { bytes_7d: 0, bytes_30d: 1024, bytes_total: 2048 },
    overridden_keys: extra.overridden_keys || [],
    invite_quota: extra.invite_quota || 0,
    invitee_count: extra.invitee_count || 0,
    effective: extra.effective || { bitrate: 20 },
    state_reason: extra.state_reason || '',
    overrides: {}, roles: [],
    ...extra,
  });

  const DB = [
    mk('u-alice', 'alice', { invitee_count: 1, inviter_id: 'u-sponsor', inviter_name: 'sponsor', tg_username: 'alice_tg', tg_user_id: '100' }),
    mk('u-bob', 'bob', {
      entitlement_state: 'expired', state: 'expired',
      expires_at: now - 86400, expires_at_effective: now - 86400,
      sync_status: 'drift', quota_source:'measured', measured_used_bytes:null,
      traffic_used_bytes:999999, metering:{source:'measured',measured_used_bytes:null,coverage:{degraded:true}},
    }),
    mk('u-carol', 'carol', {
      emby_status: 'missing', sync_status: 'emby_missing', retryable: true,
      last_remote_ok: false, last_remote_error: 'Emby unreachable',
      quota_source:'measured', measured_used_bytes:0,
      metering:{source:'measured',measured_used_bytes:0,coverage:{degraded:false}},
    }),
    mk('u-dave', 'dave', {
      group_id: 'perm', group_name: '永久', expires_at: now + 20 * 86400, expires_at_effective: null, overrides: {expires_at_override:null},
    }),
    mk('u-erin', 'erin'),
    mk('u-frank', 'frank', { emby_status: 'unknown', sync_status: 'never_applied' }),
    mk('u-gina', 'gina'),
    mk('u-hank', 'hank'),
    mk('u-ivy', 'ivy'),
    mk('u-jude', 'jude'),
    mk('u-kate', 'kate'),
    mk('u-leo', 'leo'),
  ];

  function countsOf(rows) {
    const counts = {
      total: rows.length, active: 0, expired: 0, exhausted: 0, suspended: 0, pending: 0,
      emby_missing: 0, sync_drift: 0, sync_failed: 0,
    };
    rows.forEach((m) => {
      const st = m.entitlement_state || m.state;
      if (counts[st] != null) counts[st] += 1;
      if (m.emby_status === 'missing') counts.emby_missing += 1;
      if (m.sync_status === 'drift') counts.sync_drift += 1;
      if (m.sync_status === 'failed') counts.sync_failed += 1;
    });
    return counts;
  }

  function listPayload(path) {
    const u = new URL(path, 'http://panel.local');
    const search = (u.searchParams.get('search') || '').toLowerCase();
    const status = u.searchParams.get('status') || '';
    const groupId = u.searchParams.get('group_id') || '';
    const emby = u.searchParams.get('emby_status') || '';
    const sync = u.searchParams.get('sync_status') || '';
    const sort = u.searchParams.get('sort') || 'username';
    const order = (u.searchParams.get('order') || 'asc').toLowerCase();
    let rows = DB.filter((m) => {
      if (search && !(`${m.username} ${m.tg_username || ''} ${m.emby_user_id}`).toLowerCase().includes(search)) return false;
      if (status && (m.entitlement_state || m.state) !== status) return false;
      if (groupId && m.group_id !== groupId) return false;
      if (emby && m.emby_status !== emby) return false;
      if (sync && m.sync_status !== sync) return false;
      return true;
    });
    rows = rows.slice().sort((a, b) => {
      const av = String(a[sort] || a.username || '');
      const bv = String(b[sort] || b.username || '');
      return av < bv ? -1 : av > bv ? 1 : 0;
    });
    if (order === 'desc') rows.reverse();
    const page = Number(u.searchParams.get('page') || 1);
    const size = Number(u.searchParams.get('page_size') || 50);
    const start = Math.max(0, (page - 1) * size);
    return {
      members: rows.slice(start, start + size),
      total: rows.length,
      page,
      page_size: size,
      offset: start,
      counts: countsOf(rows),
      unmanaged: [],
      unmanaged_total: 0,
    };
  }

  function detailOf(id) {
    const member = DB.find((m) => m.emby_user_id === id);
    if (!member) return null;
    return {
      member,
      devices: id === 'u-alice' ? [{ device_id: 'd1', device_name: 'TV', client: 'Emby', blocked: false }] : [],
      plays: id === 'u-alice' ? [{ item_name: 'Demo Movie' }] : [],
      audit: [{ ts: now - 60, action: 'renew', detail: '30d', ok: true }],
      points: 12,
      requests: [],
      request_remaining: 3,
      edge: member.edge,
    };
  }

  api = async (path, opts = {}) => {
    const method = String(opts.method || 'GET').toUpperCase();
    let body = null;
    if (opts.body) {
      try { body = JSON.parse(opts.body); } catch (e) { body = opts.body; }
    }
    requests.push({ path, method, body });
    const u = new URL(path, 'http://panel.local');
    const p = u.pathname;
    if (p === '/api/metering') return {config:meterConfig, totals:{period:'2026-09',by_user:{'u-alice':4096},by_node:[{node:'node-a',bytes:4096}],unattributed_bytes:0,coverage:{nodes:[{name:'node-a',ok:true,as_of:now}]}}};
    if (p === '/api/metering/cutover' && method === 'POST') { meterConfig = {...meterConfig,...body}; return meterConfig; }
    if (p === '/api/whoami') return { user: 'admin' };
    if (p === '/api/update/version') return { version: 'test' };
    if (p === '/api/groups') return GROUPS;
    if (p === '/api/emby/libraries') return [{id:'lib1',name:'Movies'}];
    if (p === '/api/enforcement/preview') return { changes: [], skipped: [] };
    if (p === '/api/members' || p === '/api/members/') return listPayload(path);
    if (p === '/api/members/bulk') return { ok: (body.user_ids || []).length, ok_flag: true, remote_failed: [] };
    if (/^\/api\/points\/.+\/adjust$/.test(p)) return {balance: 17};

    const m = p.match(/^\/api\/members\/([^/]+)(?:\/(.*))?$/);
    if (m) {
      const id = decodeURIComponent(m[1]);
      const rest = m[2] || '';
      if (rest === 'delete-preview') {
        const cascade = u.searchParams.get('cascade') === 'true';
        const available = id === 'u-alice'
          ? [{ emby_user_id: 'u-sponsor', username: 'sponsor' }] : [];
        const objects = [{ emby_user_id: id, username: (DB.find((x) => x.emby_user_id === id) || {}).username || id }];
        if (cascade) objects.push({ emby_user_id: 'u-sponsor', username: 'sponsor' });
        return {
          user_id: id,
          cascade_requested: cascade,
          default_cascade: false,
          available_cascade: available,
          objects,
        };
      }
      if (rest === 'roles' && method === 'POST') {
        const member = DB.find((x) => x.emby_user_id === id); member.roles = body.roles; return member;
      }
      if (rest === 'overrides' && method === 'PUT') {
        const member = DB.find((x) => x.emby_user_id === id); member.overrides = body;
        return {...member, ok:true, local_ok:true, remote_ok:true};
      }
      if (method === 'DELETE' && !rest) {
        lastDelete = { path, body, cascadeQuery: u.searchParams.get('cascade'), id };
        if (id === 'u-carol') {
          return {
            ok: false, deleted: false, local_ok: true, remote_ok: false, retryable: true,
            error: 'Emby unreachable',
            emby_failed: [{ user_id: id, error: 'Emby unreachable' }],
          };
        }
        return { ok: true, deleted: true, local_ok: true, remote_ok: true, retryable: false };
      }
      if (rest === 'renew-preview') {
        const member = DB.find((x) => x.emby_user_id === id) || {};
        if (!member.expires_at_effective) {
          return { allowed: false, warnings: ['永久用户不能续期'] };
        }
        return {
          allowed: true,
          current_expires_at_effective: member.expires_at_effective,
          new_expires_at: member.expires_at_effective + 30 * 86400,
          writes_override: !!(member.overridden_keys || []).length,
          warnings: [],
        };
      }
      if (rest === 'renew' && method === 'POST') {
        lastRenew = { id, body };
        return { ok: true, local_ok: true, remote_ok: true };
      }
      if (rest === 'group-preview') {
        const member = DB.find((x) => x.emby_user_id === id) || {};
        const decision = !member.expires_at_effective;
        return {
          decision_required: decision,
          warnings: decision ? ['永久/无到期账号切到计时组'] : [],
          from_group_id: member.group_id,
          to_group_id: u.searchParams.get('group_id'),
        };
      }
      if (rest === 'group' && method === 'POST') {
        lastGroup = { id, body };
        return { ok: true, local_ok: true, remote_ok: true };
      }
      if (rest === 'retry-remote' && method === 'POST') {
        lastRetry = { id };
        if (id === 'u-carol') {
          return { ok: false, local_ok: true, remote_ok: false, retryable: true, error: 'Emby still down' };
        }
        return { ok: true, local_ok: true, remote_ok: true };
      }
      if (method === 'GET' && !rest) return detailOf(id) || {};
    }
    return {};
  };

  window.api = api;
  toast = (msg, err) => { toasts.push({ msg: String(msg), err: !!err }); };
  window.confirm = (msg) => {
    confirms.push(String(msg));
    if (confirmAnswers.length) return confirmAnswers.shift();
    return true;
  };
  window.prompt = () => '30';

  const rowNames = () => [...document.querySelectorAll('#members-tbody .linkish')].map((el) => el.textContent.trim());
  const visibleText = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');

  try {
    if (typeof window.__bootPanel === 'function') bootPanel = window.__bootPanel;
    history.replaceState(null, '', '#/' + (window.__MEMBERS_HASH || 'members'));
    bootPanel();
    await waitFor(() => document.getElementById('members-page')
      && document.querySelectorAll('#members-tbody tr').length > 0,
      'initial #/members did not render');
    assert(!!document.getElementById('members-page'), 'members page missing after boot');
    assert(location.hash.indexOf('members') >= 0, 'boot hash is not members');
    assert(rowNames().includes('alice'), 'alice missing on first load');
    assert(rowNames().includes('carol'), 'carol missing on first load');

    const carolRow = document.querySelector('tr[data-id="u-carol"]');
    assert(carolRow, 'carol row missing');
    const carolText = visibleText(carolRow);
    assert(carolText.includes('账号缺失'), 'missing Emby still looks enabled: ' + carolText);
    assert(!/可正常播放|已启用/.test(carolText), 'missing Emby shown as playable');
    assert(carolText.includes('正常') || carolText.includes('权益'), 'carol entitlement missing');
    assert(carolRow.querySelector('[data-act="retry"]'), 'retry button missing for retryable carol');
    assert(visibleText(document.querySelector('tr[data-id="u-bob"]')).includes('实测配额 未测'), 'missing measured quota fell back to legacy estimate or zero');
    assert(carolText.includes('实测配额 0 B'), 'known measured zero was not distinguished from missing');

    const aliceRow = document.querySelector('tr[data-id="u-alice"]');
    assert(aliceRow && /Emby 在线/.test(visibleText(aliceRow)), 'alice emby present not shown');

    await go('members?q=alice&page=1&page_size=5');
    await tick();
    await waitFor(() => rowNames().includes('alice') && rowNames().length === 1,
      'deep link search did not isolate alice: ' + rowNames().join(','));
    assert(document.getElementById('m-q').value === 'alice', 'search input not synced from hash');
    assert(location.hash.indexOf('q=alice') >= 0, 'hash lost q=alice');

    await go('members?status=expired&page_size=5');
    await tick();
    await waitFor(() => rowNames().includes('bob') && !rowNames().includes('alice'),
      'status filter failed: ' + rowNames().join(','));

    await go('members?emby_status=missing&page_size=5');
    await tick();
    await waitFor(() => rowNames().join() === 'carol', 'emby_status filter failed: ' + rowNames().join(','));

    await go('members?sort=username&order=desc&page_size=5&page=1');
    await tick();
    await waitFor(() => rowNames().length === 5, 'sort page did not load');
    const desc = rowNames();
    assert(desc[0] > desc[desc.length - 1] || desc[0] === 'leo', 'desc sort unexpected: ' + desc.join(','));

    await go('members?page_size=5&page=1');
    await tick();
    await waitFor(() => rowNames().length === 5, 'page 1 size 5 failed');
    const page1 = rowNames();
    await go('members?page_size=5&page=2');
    await tick();
    await waitFor(() => /page=2/.test(location.hash) && rowNames().length === 5,
      'go page 2 did not load: ' + location.hash + ' ' + rowNames().join(','));
    const page2 = rowNames();
    page1.forEach((name) => assert(!page2.includes(name), 'page 2 overlaps page 1 with ' + name));
    await go('members?page_size=5&page=1');
    await tick();
    await waitFor(() => rowNames().join() === page1.join(), 'return page 1');
    document.querySelector('[data-act="page"][data-page="2"]').click();
    await waitFor(() => /page=2/.test(location.hash) && rowNames().join() === page2.join(),
      'pager click did not load page 2: ' + location.hash + ' ' + rowNames().join(','));

    history.back();
    await waitFor(() => /page=1/.test(location.hash) && rowNames().join() === page1.join(),
      'browser back did not restore page 1: ' + location.hash + ' ' + rowNames().join(','));
    history.forward();
    await waitFor(() => /page=2/.test(location.hash) && rowNames().join() === page2.join(),
      'browser forward did not restore page 2');

    await go('members?page_size=50');
    await tick();
    await waitFor(() => document.querySelector('tr[data-id="u-alice"]'), 'reload all rows');

    document.querySelector('[data-id="u-alice"][data-act="open"]').click();
    await waitFor(() => document.querySelector('#member-detail h3')
      && document.querySelector('#member-detail h3').textContent === 'alice', 'alice detail missing');
    assert(!document.getElementById('member-detail').classList.contains('hidden'), 'detail hidden');
    assert(/权益/.test(visibleText(document.getElementById('member-detail'))), 'overview tab missing 权益');

    const tabBtns = [...document.querySelectorAll('#member-detail [data-tab]')].map((b) => b.dataset.tab);
    assert(tabBtns.join() === 'overview,entitlements,devices,invites,audit', 'tabs: ' + tabBtns.join());
    for (const tab of ['entitlements', 'devices', 'invites', 'audit']) {
      document.querySelector(`#member-detail [data-tab="${tab}"]`).click();
      await waitFor(() => document.querySelector(`#member-detail [data-tab="${tab}"].active`),
        'tab ' + tab + ' not active');
      assert(document.querySelector('#member-detail .card-body'), 'tab body missing ' + tab);
      if (tab === 'entitlements') {
        assert(document.getElementById('ov-streams') && document.getElementById('ov-save'), 'permission editor was removed');
        assert(document.querySelectorAll('.md-role').length === 2, 'role editor was removed');
        document.getElementById('ov-streams').value = '3';
        document.getElementById('ov-exp-mode').value = 'forever';
        confirmAnswers.splice(0, confirmAnswers.length, true);
        document.getElementById('ov-save').click();
        await waitFor(() => requests.some((r) => r.path.endsWith('/overrides') && r.method === 'PUT'), 'override save missing');
        const saved = requests.filter((r) => r.path.endsWith('/overrides') && r.method === 'PUT').at(-1).body;
        assert(saved.max_streams === 3 && saved.expires_at_override === null, 'override save lost explicit unlimited meaning');
        await tick();
      }
      if (tab === 'devices') assert(document.querySelector('[data-forget-device]'), 'device removal entry was removed');
      if (tab === 'invites') assert(document.getElementById('md-points-save'), 'points adjustment entry was removed');
    }
    document.querySelector('#member-detail [data-tab="overview"]').click();
    await waitFor(() => document.getElementById('md-renew'), 'overview actions missing');
    ['password','kick','status','reset-traffic','telegram/unbind'].forEach((action) =>
      assert(document.querySelector(`[data-member-action="${action}"]`), 'account action removed: ' + action));

    confirmAnswers.splice(0, confirmAnswers.length, true);
    document.getElementById('md-renew').click();
    await waitFor(() => lastRenew && lastRenew.id === 'u-alice', 'renew was not posted');
    assert(toasts.some((t) => t.msg.includes('已续期') && !t.err), 'renew success toast missing');

    document.querySelector('[data-id="u-dave"][data-act="open"]').click();
    await waitFor(() => document.querySelector('#member-detail h3')
      && document.querySelector('#member-detail h3').textContent === 'dave', 'dave detail missing');
    const toastCount = toasts.length;
    confirmAnswers.splice(0, confirmAnswers.length, true);
    document.getElementById('md-renew').click();
    await waitFor(() => toasts.length > toastCount, 'permanent renew gave no toast');
    assert(toasts.some((t) => t.err && /永久/.test(t.msg)), 'permanent renew must not succeed: '
      + JSON.stringify(toasts.slice(-3)));

    document.getElementById('md-group').value = 'standard';
    document.getElementById('md-policy').value = 'keep';
    confirmAnswers.splice(0, confirmAnswers.length, true);
    document.getElementById('md-group-go').click();
    await waitFor(() => lastGroup && lastGroup.id === 'u-dave', 'group change was not posted');
    assert(lastGroup.body.expiry_policy === 'keep', 'group policy not keep: ' + JSON.stringify(lastGroup.body));

    toasts.length = 0;
    confirmAnswers.splice(0, confirmAnswers.length, false);
    document.querySelector('[data-id="u-carol"][data-act="delete"]').click();
    await tick();
    assert(lastDelete === null, 'cancelled self deletion still sent a DELETE');
    confirmAnswers.splice(0, confirmAnswers.length, true);
    document.querySelector('[data-id="u-carol"][data-act="delete"]').click();
    await waitFor(() => lastDelete && lastDelete.id === 'u-carol', 'carol confirmed delete not sent');
    assert(lastDelete.cascadeQuery !== 'true' && !(lastDelete.body && lastDelete.body.cascade),
      'default delete sent cascade: ' + JSON.stringify(lastDelete));
    assert(toasts.some((t) => t.err && /Emby/.test(t.msg)), 'remote delete failure not toasted');
    assert(!toasts.some((t) => !t.err && t.msg.includes('已删除')), 'failed delete still toasted success');

    lastDelete = null;
    confirms.length = 0;
    confirmAnswers.splice(0, confirmAnswers.length, false);
    document.querySelector('[data-id="u-alice"][data-act="delete"]').click();
    await tick();
    assert(lastDelete === null, 'cancelled deletion of an invited member still deleted them');
    confirmAnswers.splice(0, confirmAnswers.length, true);
    document.querySelector('[data-id="u-alice"][data-act="delete"]').click();
    await waitFor(() => lastDelete && lastDelete.id === 'u-alice', 'default self delete missing');
    assert(lastDelete.body.cascade === false && lastDelete.body.confirm_ids.join() === 'u-alice', 'default self deletion included inviter');
    lastDelete = null;
    confirmAnswers.splice(0, confirmAnswers.length, false);
    document.querySelector('[data-id="u-alice"][data-act="delete-cascade"]').click();
    await tick();
    assert(lastDelete === null, 'cancelled cascade still deleted an account');
    confirmAnswers.splice(0, confirmAnswers.length, true);
    document.querySelector('[data-id="u-alice"][data-act="delete-cascade"]').click();
    await waitFor(() => lastDelete && lastDelete.id === 'u-alice' && lastDelete.body
      && lastDelete.body.cascade === true, 'cascade delete not sent');
    const ids = (lastDelete.body.confirm_ids || []).slice().sort();
    assert(ids.join() === 'u-alice,u-sponsor', 'confirm_ids mismatch: ' + ids.join());
    assert(lastDelete.cascadeQuery === 'true', 'cascade query missing');

    await go('members?page_size=50');
    await tick();
    await waitFor(() => document.getElementById('members-page'), 'reload after deletes');
    document.querySelector('[data-id="u-alice"][data-act="open"]').click();
    await waitFor(() => document.querySelector('#member-detail h3'), 'reopen detail');

    const search = document.getElementById('m-q');
    search.value = 'draft-keep';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    search.blur();
    const box = document.querySelector('tr[data-id="u-bob"] .m-pick');
    box.checked = true;
    box.dispatchEvent(new Event('change', { bubbles: true }));
    box.blur();
    search.focus(); search.setSelectionRange(2, 5);
    const wrap = document.getElementById('members-table-wrap');
    const scrollStyle = document.createElement('style');
    scrollStyle.textContent = '#members-table-wrap {max-height:180px;overflow:auto}';
    document.head.appendChild(scrollStyle);
    wrap.scrollTop = 70;
    const scrollBefore = wrap.scrollTop;
    const detail = document.getElementById('member-detail');
    const heading = detail.querySelector('h3');
    const selectedBefore = document.getElementById('m-sel-count').textContent;
    assert(/已选 1 人/.test(selectedBefore), 'selection count: ' + selectedBefore);

    const beforeReq = requests.length;
    const source = sources.filter((s) => /topics=members/.test(s.url)).at(-1);
    assert(source, 'members EventSource was not opened: ' + sources.map((s) => s.url).join(','));
    source.emit('members', { total: 12, counts: countsOf(DB), pulse: [] });
    source.emit('members', { total: 12, counts: countsOf(DB), pulse: [{ id: 'u-alice', state: 'active' }] });
    await tick(250);

    assert(document.getElementById('members-page'), 'live update removed members page');
    assert(document.getElementById('m-q') === search, 'live update replaced search input');
    assert(search.value === 'draft-keep', 'live update wiped search draft');
    assert(document.activeElement === search && search.selectionStart === 2 && search.selectionEnd === 5, 'live update lost editing focus/caret');
    assert(document.getElementById('member-detail') === detail, 'live update replaced detail node');
    assert(detail.contains(heading) && heading.textContent === 'alice', 'live update closed/rebuilt detail');
    assert(box.isConnected && box.checked, 'live update lost checkbox');
    assert(/已选 1 人/.test(document.getElementById('m-sel-count').textContent), 'live update lost selection count');
    assert(scrollBefore > 0 && document.getElementById('members-table-wrap').scrollTop === scrollBefore,
      'live update changed table scroll ' + document.getElementById('members-table-wrap').scrollTop);
    const memberGets = requests.slice(beforeReq).filter((r) => r.path.startsWith('/api/members?')).length;
    assert(memberGets >= 1, 'members topic did not refresh while an input was focused');
    const unrelated = requests.slice(beforeReq).filter((r) =>
      r.path.startsWith('/api/nodes') || r.path.startsWith('/api/pipeline') || r.path.startsWith('/api/sessions'));
    assert(unrelated.length === 0, 'live update fetched unrelated APIs');

    search.blur();
    document.querySelector('[data-act="metering"]').click();
    await waitFor(() => document.getElementById('meter-cutover'), 'metering preview missing');
    document.getElementById('meter-cutover').click(); await tick();
    assert(!requests.some((r) => r.path === '/api/metering/cutover'), 'cutover enabled without baseline confirmation');
    document.getElementById('meter-baseline').checked = true;
    confirmAnswers.splice(0, confirmAnswers.length, false);
    document.getElementById('meter-cutover').click(); await tick();
    assert(!requests.some((r) => r.path === '/api/metering/cutover'), 'cancelled cutover still wrote configuration');
    confirmAnswers.splice(0, confirmAnswers.length, true);
    document.getElementById('meter-cutover').click();
    await waitFor(() => requests.some((r) => r.path === '/api/metering/cutover'), 'confirmed cutover not submitted');
    const activation = requests.find((r) => r.path === '/api/metering/cutover').body;
    assert(activation.cutover === true && activation.baseline_confirmed === true, 'cutover payload lost explicit confirmation');
    await tick();
    await go('groups'); await tick();
    assert(document.getElementById('new-bandwidth'), 'group page broke after removing shared bandwidth presets');
    await go('members?page_size=50'); await tick();
    report.textContent = JSON.stringify({
      ok: true,
      checks,
      rows: rowNames(),
      hash: location.hash,
      lastDelete,
      lastRenew,
      lastGroup,
    });
  } catch (error) {
    report.textContent = JSON.stringify({
      ok: false,
      checks,
      error: String(error && error.message || error),
      hash: location.hash,
      rows: rowNames(),
      ready: typeof state !== 'undefined' ? {page: state.page, route: state.route, pageReady: state.pageReady, ver: state.renderVersion} : null,
      view: (document.getElementById('view') || {}).innerHTML
        ? String(document.getElementById('view').innerHTML).slice(0, 400)
        : '',
      toasts,
      lastDelete,
      lastRenew,
      lastGroup,
      requests: requests.slice(-12),
    });
  }
})();
