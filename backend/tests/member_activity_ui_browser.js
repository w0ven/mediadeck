/* Real scripts/Chromium, real API projections, only isolated mock API/SSE I/O. */
(async () => {
  const report = document.getElementById('report');
  let checks = 0;
  const requests = [];
  const sources = [];
  const assert = (v, label) => { if (!v) throw new Error(label); checks++; };
  const tick = () => new Promise(resolve => setTimeout(resolve, 40));
  async function waitFor(pred, label) {
    for (let i = 0; i < 100; i++) { if (pred()) return; await tick(); }
    throw new Error(label);
  }
  class FakeSource {
    constructor(url) { this.url = url; this.handlers = {}; sources.push(this); }
    addEventListener(topic, fn) { this.handlers[topic] = fn; }
    emit(topic) { this.handlers[topic]?.({data: '{}'}); }
    close() {}
  }
  window.EventSource = FakeSource;
  const rows = window.__ACTIVITY_ROWS;
  const names = () => [...document.querySelectorAll('#members-tbody tr')].map(el => el.dataset.id);
  const row = id => document.querySelector(`#members-tbody tr[data-id="${id}"]`);
  const playbackCell = id => row(id)?.querySelector('.last-played').parentElement;
  const play = id => row(id)?.querySelector('.last-played').textContent;
  const params = () => new URLSearchParams(location.hash.split('?')[1] || '');
  const set = (id, value) => {
    const el = document.getElementById(id);
    el.value = value;
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const bulkHidden = () => document.getElementById('members-bulk').classList.contains('hidden');
  function listing(p) {
    let result = rows.filter(m => {
      if (p.get('activity') && m.playback_activity.status !== p.get('activity')) return false;
      if (p.get('status') && m.state !== p.get('status')) return false;
      if (p.get('group_id') && m.group_id !== p.get('group_id')) return false;
      if (p.get('role') && !m.roles.includes(p.get('role'))) return false;
      if (p.get('tg') === 'bound' && !m.tg_user_id) return false;
      if (p.get('tg') === 'unbound' && m.tg_user_id) return false;
      if (p.get('search') && !m.username.includes(p.get('search'))) return false;
      return true;
    });
    const sort = p.get('sort');
    result.sort((a, b) => ['last_seen', 'last_played'].includes(sort)
      ? (a.last_played_at || 0) - (b.last_played_at || 0)
      : a.username.localeCompare(b.username));
    if (p.get('order') === 'desc') result.reverse();
    const page = Number(p.get('page') || 1), size = Number(p.get('page_size') || 50);
    const counts = {total: result.length};
    result.forEach(m => { counts[m.state] = (counts[m.state] || 0) + 1; });
    return {members: result.slice((page - 1) * size, page * size), total: result.length,
      page, page_size: size, counts, unmanaged: []};
  }
  api = async (path, opts = {}) => {
    requests.push({path, method: opts.method || 'GET', body: opts.body && JSON.parse(opts.body)});
    const url = new URL(path, 'http://isolated.panel');
    if (url.pathname === '/api/members') return listing(url.searchParams);
    if (url.pathname === '/api/groups') return window.__ACTIVITY_GROUPS;
    if (url.pathname === '/api/members/bulk') return {ok: JSON.parse(opts.body).user_ids.length};
    if (url.pathname === '/api/whoami') return {user: 'admin'};
    if (url.pathname === '/api/update/version') return {version: 'test'};
    return {};
  };
  window.api = api;
  toast = () => {};
  window.deckConfirm = async () => true;
  window.deckPrompt = async () => '30';
  try {
    bootPanel = window.__bootPanel;
    history.replaceState(null, '', '#/members');
    bootPanel();
    await waitFor(() => names().length === 7, 'initial rows');
    assert([...document.querySelectorAll('#members-table th')].some(el => el.textContent === '最后播放'), 'last playback header');
    assert(document.getElementById('m-activity').getAttribute('aria-label') === '活跃度', 'activity control label');
    assert(document.querySelector('#m-activity option[value="pending"]').textContent === '\u672a\u5f00\u901a', 'pending activity option must be 未开通, without replacement characters');
    assert(play('alpha') === '40.0 天前', 'new access must not replace old playback: ' + play('alpha'));
    assert(play('zeta') === '暂无播放记录', 'no record must not assert never played');
    assert(play('eta') === '数据不可用', 'failed playback source remains unavailable');
    for (const id of names()) {
      const cell = playbackCell(id);
      assert(cell.textContent.trim() === play(id), 'last playback cell contains only elapsed time or the missing-data label: ' + id);
      assert(cell.children.length === 1 && !cell.querySelector('[title],.playback-activity'), 'no observation/score/formula attached to last playback: ' + id);
    }
    set('m-activity', 'inactive');
    await waitFor(() => names().join() === 'alpha,beta,zeta', 'activity filter');
    assert(params().get('activity') === 'inactive' && params().get('page') === '1', 'filter hash/page reset');
    assert(requests.at(-1).path.includes('activity=inactive') || requests.some(r => r.path.includes('activity=inactive')), 'API query forwarding');
    assert(playbackCell('beta').textContent.trim() === play('beta'), 'candidate filter does not add scores to playback time');
    set('m-sort', 'last_seen');
    await waitFor(() => params().get('sort') === 'last_seen' && names().join() === 'zeta,alpha,beta', 'ascending playback sort');
    set('m-order', 'desc');
    await waitFor(() => names().join() === 'beta,alpha,zeta', 'descending playback sort');
    await go('members?activity=inactive&sort=last_seen&order=desc&page_size=1&page=1');
    await waitFor(() => names().join() === 'beta', 'page one');
    const pick = row('beta').querySelector('.m-pick');
    pick.checked = true; pick.dispatchEvent(new Event('change', {bubbles: true}));
    assert(!bulkHidden(), 'selection visible');
    document.querySelector('[data-act="page"][data-page="2"]').click();
    await waitFor(() => names().join() === 'alpha' && params().get('page') === '2', 'next page');
    assert(bulkHidden(), 'changing page clears selections');
    assert(params().get('activity') === 'inactive' && document.getElementById('m-activity').value === 'inactive', 'pagination keeps activity');
    assert(document.getElementById('members-pager').textContent.includes('3 人'), 'total before pagination');
    set('m-group', 'standard');
    await waitFor(() => names().join() === 'beta' && params().get('page') === '1', 'combining group resets page');
    assert(params().get('activity') === 'inactive' && params().get('group_id') === 'standard', 'combine group + activity');
    set('m-tg', 'bound');
    await waitFor(() => params().get('tg') === 'bound', 'tg filter');
    set('m-role', 'admin');
    await waitFor(() => params().get('role') === 'admin', 'role filter');
    set('m-status', 'pending');
    await waitFor(() => !!document.getElementById('members-empty'), 'incompatible combined filter is empty');
    assert(document.getElementById('m-activity').value === 'inactive', 'empty result keeps activity filter');
    document.getElementById('m-reset').click();
    await waitFor(() => names().length === 7 && !params().get('activity'), 'clear all filters');
    assert(document.getElementById('m-group').value === '' && document.getElementById('m-status').value === '', 'reset clears combination');
    await go('members?activity=inactive&group_id=standard&sort=last_seen&order=desc&page_size=50');
    await waitFor(() => names().join() === 'beta,alpha', 'filtered bulk cohort');
    const selected = row('beta').querySelector('.m-pick');
    selected.checked = true; selected.dispatchEvent(new Event('change', {bubbles: true}));
    const source = sources.filter(s => s.url.includes('topics=members')).at(-1);
    assert(source, 'members live source');
    source.emit('members');
    await tick(); await tick();
    assert(document.getElementById('m-activity').value === 'inactive' && selected.checked, 'live refresh keeps filter/selection');
    document.querySelector('[data-act="bulk"][data-bulk="renew"]').click();
    await waitFor(() => requests.some(r => r.path === '/api/members/bulk'), 'mocked explicit bulk');
    const body = requests.find(r => r.path === '/api/members/bulk').body;
    assert(body.user_ids.join() === 'beta' && body.days === 30, 'only explicitly selected current user submitted');
    await waitFor(bulkHidden, 'completed bulk clears selection');
    set('m-activity', 'observing');
    await waitFor(() => names().join() === 'delta', 'observing filter');
    assert(playbackCell('delta').textContent.trim() === play('delta'), 'observation filter does not alter playback-only column');
    document.getElementById('m-reset').click();
    await waitFor(() => names().length === 7, 'reset before unavailable');
    set('m-activity', 'unavailable');
    await waitFor(() => names().join() === 'eta', 'unknown source filter');
    assert(bulkHidden(), 'filter changes do not select inactive users automatically');
    assert(requests.filter(r => r.method !== 'GET').length === 1, 'activity has no automatic mutation');
    report.textContent = JSON.stringify({ok: true, checks});
  } catch (error) {
    report.textContent = JSON.stringify({ok: false, checks, error: String(error), hash: location.hash,
      names: names(), requests: requests.slice(-8)});
  }
})();
