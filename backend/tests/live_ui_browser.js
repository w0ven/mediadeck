/* Real Chromium, real panel code, controlled API/SSE; no production requests. */
(async () => {
  const report = document.getElementById('report');
  let checks = 0;
  const assert = (value, label) => { if (!value) throw new Error(label); checks++; };
  const tick = (ms = 80) => new Promise((resolve) => setTimeout(resolve, ms));
  const requests = [];
  const sources = [];
  class FakeSource {
    constructor(url) { this.url = url; this.handlers = {}; this.closed = false; sources.push(this); }
    addEventListener(topic, handler) { this.handlers[topic] = handler; }
    emit(topic, data) { if (this.handlers[topic]) this.handlers[topic]({data: JSON.stringify(data)}); }
    close() { this.closed = true; }
  }
  window.EventSource = FakeSource;
  const node = {name: 'node-a', available: true, ok: true, enabled: true, capacity: 10,
    active_streams: 1, egress_mbps: 8, utilisation: 0.1, pools: [], base_url: 'https://node.example',
    cache_dir: '/cache', cache_size: '1T', sign_secret_set: true, enrolled: true};
  let nodes = [node];
  let snapshot = {available: true, snapshot_age_seconds: 1, stale: false,
    data: {generated_at: '2026-09-10T21:59:00+0800', queues: [], quota: [], alerts: [], mounts: [], tasks: []}};
  let delayDashboard = null;
  api = async (path) => {
    requests.push(path);
    if (path === '/api/nodes') return structuredClone(nodes);
    if (path.startsWith('/api/dispatch/log')) return [];
    if (path === '/api/settings/dispatch') return {policy: 'affinity', load_threshold: 0.4};
    if (path === '/api/settings') return {integration: {panel_public_url: 'https://panel.example'}};
    if (path === '/api/emby/sessions') {
      if (delayDashboard) return delayDashboard;
      return [{Id:'s1', UserId:'u1', UserName:'User 1', Client:'Client', SpeedBps:1000,
        SpeedMBps:0.1, SpeedSource:'node', ItemId:'m1', ProgressPercent:10}];
    }
    if (path === '/api/emby/libraries' || path.startsWith('/api/emby/latest')) return [];
    if (path.startsWith('/api/stats/overview')) return {members: {total:1,active:1}};
    if (['/api/pipeline','/api/tasks','/api/mounts','/api/intake'].includes(path)) return structuredClone(snapshot);
    if (path === '/api/storage/mounts') return [];
    return {};
  };
  toast = () => {};
  fillNodeMounts = () => {};
  try {
    go('nodes'); await tick();
    const input = document.getElementById('nd-name');
    input.value = 'unsaved node'; input.focus(); input.setSelectionRange(3,7);
    input.dispatchEvent(new Event('input', {bubbles:true})); input.blur();
    const nodeCard = input.closest('.card');
    const mount = document.getElementById('nmounts-node-a');
    mount.innerHTML = '<input type="checkbox" id="keep-checked" checked><span>loaded mounts</span>';
    const checked = document.getElementById('keep-checked');
    const view = document.getElementById('view');
    view.style.height = '260px'; view.style.overflow = 'auto'; view.scrollTop = 80;
    const scrollBefore = view.scrollTop;
    const enroll = document.getElementById('enroll-node-a');
    enroll.innerHTML = '<pre id="open-enrollment">installation preview</pre>';
    const openEnrollment = document.getElementById('open-enrollment');
    const beforeRequests = requests.length;
    const source = sources.at(-1);
    source.emit('nodes', [{...node, active_streams:3, egress_mbps:24}]);
    source.emit('nodes', [{...node, active_streams:4, egress_mbps:32}]);
    await tick();
    assert(document.getElementById('nd-name') === input, 'SSE replaced the node form DOM');
    assert(input.value === 'unsaved node', 'SSE erased an unfocused unsaved form');
    assert(input.closest('.card') === nodeCard, 'SSE replaced stable card');
    assert(document.getElementById('keep-checked') === checked && checked.checked, 'SSE erased loaded/selected mounts');
    assert(requests.length === beforeRequests, 'SSE refetched unrelated REST endpoints');
    assert(scrollBefore > 0 && view.scrollTop === scrollBefore, 'SSE changed scroll position');
    assert(document.getElementById('open-enrollment') === openEnrollment, 'SSE erased expanded enrollment preview');
    assert(document.querySelectorAll('#view .stat .val')[1].textContent === '4', 'latest node value did not update');
    input.focus(); input.setSelectionRange(2,5);
    source.emit('nodes', [{...node, active_streams:5, egress_mbps:40}]); await tick();
    assert(document.activeElement === input && input.selectionStart === 2 && input.selectionEnd === 5, 'SSE lost input focus/caret');
    assert(document.querySelectorAll('#view .stat .val')[1].textContent === '5', 'focused form blocked live metrics');

    input.blur(); go('dashboard'); await tick();
    const dashboardSource = sources.at(-1);
    const play = document.querySelector('.play-card');
    const poster = document.querySelector('.play-card img');
    const dashboardRequests = requests.length;
    dashboardSource.emit('sessions', [{Id:'s1',UserId:'u1',UserName:'User 1',Client:'Client',
      SpeedBps:2000,SpeedMBps:0.2,SpeedSource:'node',ItemId:'m1',ProgressPercent:20},
      {Id:'s2',UserId:'u2',UserName:'User 2',Client:'Client',SpeedBps:0,SpeedMBps:0,SpeedSource:'node'}]);
    await tick();
    assert(document.querySelector('.play-card') === play, 'stable playback row was replaced');
    assert(document.querySelector('.play-card img') === poster, 'unchanged poster reloaded');
    assert(document.querySelectorAll('.play-card').length === 2, 'new playback row not added');
    assert(requests.length === dashboardRequests, 'dashboard event refetched library/overview');
    dashboardSource.emit('sessions', [{Id:'s2',UserId:'u2',UserName:'User 2',Client:'Client',SpeedBps:0,SpeedMBps:0,SpeedSource:'node'}]);
    await tick();
    assert(document.querySelectorAll('.play-card').length === 1 && document.querySelector('.play-card').textContent.includes('User 2'), 'removed playback row remains');

    for (const page of ['pipeline','tasks','mounts','intake']) {
      go(page); await tick();
      const card = document.querySelector('#view .card');
      const requestCount = requests.length;
      sources.at(-1).emit(page, {...snapshot, snapshot_age_seconds:9}); await tick();
      assert(document.querySelector('#view .card') === card, page + ' replaced stable DOM');
      assert(requests.length === requestCount, page + ' refetched on live event');
    }

    snapshot = {...snapshot, snapshot_age_seconds: 7200, stale: true,
      data: {...snapshot.data, alerts: [{level:'warn', message:'file modification time > 12h'}]}};
    go('pipeline'); await tick();
    const staleText = document.querySelector('#view').textContent;
    assert(staleText.includes('快照已过期'), 'stale pipeline status is not explicit');
    assert(staleText.includes('历史采集') && staleText.includes('不代表当前状态'),
      'stale pipeline values are not marked historical');
    assert(staleText.includes('快照文件更新时间'), 'stale status does not identify file mtime basis');

    let received = 0;
    PAGES.members = async () => { document.getElementById('view').innerHTML = '<div id="custom-member-page">members</div>'; };
    if (typeof registerLiveUpdater === 'function') registerLiveUpdater('members', ['members'], (topic,payload,ctx) => {
      if (ctx.isCurrent()) received = payload.revision;
    });
    go('members?q=test&page=2'); await tick();
    assert(state.page === 'members' && location.hash.includes('q=test'), 'query route treated as page name');
    const memberPage = document.getElementById('custom-member-page');
    sources.at(-1).emit('members', {revision:3}); await tick();
    assert(received === 3 && document.getElementById('custom-member-page') === memberPage, 'custom updater contract failed');
    source.emit('nodes', [{...node,active_streams:99}]); await tick();
    assert(document.getElementById('custom-member-page') === memberPage, 'closed source overwrote current page');

    let resolveOld;
    delayDashboard = new Promise((resolve) => {resolveOld=resolve;});
    go('dashboard'); await tick();
    go('members?q=new'); await tick();
    const current = document.getElementById('custom-member-page');
    resolveOld([]); await tick();
    assert(document.getElementById('custom-member-page') === current, 'late page request overwrote newer route');
    assert(location.hash.includes('q=new'), 'late page reset route query');
    delayDashboard = null;
    go('nodes'); await tick();
    const broken = sources.at(-1);
    broken.onerror(); broken.onerror();
    const afterDisconnect = sources.length;
    go('tasks'); await tick();
    const afterNavigation = sources.length;
    await tick(1100);
    assert(afterNavigation === afterDisconnect + 1 && sources.length === afterNavigation, 'old reconnect timer created another source after navigation');
    broken.onopen();
    assert(live.src === sources.at(-1), 'old source reattached after navigation');
    go('members?q=first'); await tick();
    location.hash = '#/members?q=second'; await tick();
    assert(state.page === 'members' && state.route === 'members?q=second', 'query-only hash navigation failed');

    assert(!sessionSpeedCell({Paused:true,SpeedSource:'node',SpeedBps:1048576,SpeedMBps:1}).startsWith('<span class="muted">0'), 'paused playback hid measured traffic');
    assert(!sessionSpeedCell({SpeedSource:'unknown',SpeedBps:null,SpeedMBps:null}).includes('0.0'), 'unknown measurement shown as zero');
    report.textContent = JSON.stringify({ok:true,checks});
  } catch (error) {
    report.textContent = JSON.stringify({ok:false,checks,error:String(error.stack || error)});
  }
})();
