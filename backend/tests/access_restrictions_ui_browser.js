/* Offline regression: real access page, API helper, async buttons and deckConfirm. */
(async () => {
  let checks = 0;
  const assert = (ok, label) => { if (!ok) throw new Error(label); checks++; };
  const report = document.querySelector('#report');
  const clone = value => structuredClone(value);
  const messages = [];
  toast = (message, bad) => messages.push({message, bad});
  const evil = '\"><img src=x onerror="window.injected=true">\'&';
  const initialConfig = {
    concurrency: {enabled: true, action: 'pause'},
    web_login: {enabled: false, action: 'disable'},
    web_play: {enabled: false, action: 'disable'},
  };
  const noticeStates = ['sent', 'retry', 'failed', 'unknown', 'pending', 'sending', 'skipped'];
  let data = {config: clone(initialConfig), events: ['done', 'failed', 'unknown', 'pending'].map((status, i) => ({
    id: i + 1, created_at: Date.now() / 1000 - 120, username: 'member-' + i, user_id: 'user-' + i,
    rule: ['concurrency', 'web_login', 'web_play'][i % 3], action: ['pause', 'disable', 'delete'][i % 3],
    status, result: i === 0 ? {detail: 'only rejected excess playback'} : 'punishment result ' + i,
    notices: i === 0 ? noticeStates.map((state, n) => ({
      kind: n % 2 ? 'private' : 'group:-100987654321', state, attempts: n,
      error: state === 'failed' ? 'TG not bound / bot blocked' : '',
    })) : [],
  }))};
  data.events.push({id: evil, created_at: 0, username: evil, user_id: evil, rule: evil,
    action: evil, status: evil, result: {detail: evil},
    notices: [{kind: 'private', state: evil, attempts: evil, error: evil}]});
  const legacyRules = [{id: 91, kind: 'client', pattern: 'curl|wget', action: 'deny', note: 'legacy rule', enabled: true}];
  const legacyBlocks = [{blocked_at: Date.now() / 1000, username: 'legacy-block', user_agent: 'curl',
    remote_ip: '203.0.113.3', reason: 'legacy rejection', rule_id: 91}];
  const requests = [];
  let readError = '', writeError = '', normalizeWrite = false, readGate = null, writeGate = null;
  const response = value => ({ok: true, json: async () => clone(value)});
  const failure = detail => ({ok: false, status: 503, json: async () => ({detail})});
  window.fetch = async (path, opts = {}) => {
    const method = opts.method || 'GET';
    requests.push({path, method, body: opts.body ? JSON.parse(opts.body) : undefined});
    if (path === '/api/access/rules' && method === 'GET') return response(legacyRules);
    if (path === '/api/access/blocks?limit=100' && method === 'GET') return response(legacyBlocks);
    if (path !== '/api/access/restrictions') throw new Error('Unexpected request: ' + method + ' ' + path);
    if (method === 'GET') {
      if (readGate) return readGate;
      return readError ? failure(readError) : response(data);
    }
    if (method !== 'PUT') throw new Error('Unexpected restriction method: ' + method);
    if (writeGate) return writeGate;
    if (writeError) return failure(writeError);
    data.config = JSON.parse(opts.body);
    if (normalizeWrite) { data.config.web_login.enabled = false; normalizeWrite = false; }
    return response(data.config);
  };
  const writes = () => requests.filter(r => r.method === 'PUT');
  const select = key => $('#acr-' + key + '-action');
  const enabled = key => $('#acr-' + key + '-enabled');
  const save = () => $('#ac-restrictions-save').onclick();
  const refresh = () => $('#ac-restrictions-refresh').onclick();
  const activeDialog = () => document.querySelector('.deck-dialog-backdrop:not(.deck-dialog-closing)');
  const load = async () => {
    state.page = 'access'; state.route = 'access'; state.renderVersion = (state.renderVersion || 0) + 1;
    await PAGES.access(pageContext('access'));
  };
  try {
    await load();
    assert(requests.length === 3 && writes().length === 0, 'load is exactly three read-only endpoints');
    assert([...$('#view').querySelectorAll('.card h3')].slice(0, 3).map(el => el.textContent).join('|') ===
      '账号访问限制|账号处罚记录|新增规则', 'new cards are above unchanged generic rules');
    assert(enabled('concurrency').checked && !enabled('web_login').checked && !enabled('web_play').checked,
      'all three switches reflect GET config');
    assert(select('concurrency').value === 'pause' && select('web_login').value === 'disable', 'GET actions reflected');
    assert([...$('#ac-restrictions').querySelectorAll('select')].every(el =>
      [...el.options].map(o => o.value).join('|') === 'pause|disable|delete'), 'all rules have all three legal actions');
    const help = $('#ac-restrictions').textContent;
    for (const text of ['max_streams', '已有合规播放不动', '拒绝此次登录', 'Emby 管理员或 Deck admin 豁免',
      '白名单仍按自身规则', '已验证的 Emby 客户端身份（Emby Web）', '不只凭浏览器 UA',
      '密码错误／未登录访问不处罚', '已配置互动群', '用户绑定 TG 私信', '未绑定 TG／Bot 被屏蔽会记录失败',
      '已发云盘 URL 不可回收']) assert(help.includes(text), 'required explanation: ' + text);
    assert($('#ac-pattern') && $('#view').textContent.includes('curl|wget') &&
      $('#view').textContent.includes('legacy-block'), 'generic UA/IP rule form, rules and block log preserved');
    const events = $('#ac-restriction-events');
    assert(events.querySelectorAll('tbody tr').length === 5, 'all returned events rendered');
    assert(events.textContent.includes('TG 私信') && events.textContent.includes('互动群') &&
      !events.textContent.includes('-100987654321'), 'notice destination labels do not expose chat ID');
    for (const text of ['已执行', '执行失败', '结果不明', '待执行', '已送达', '等待重试', '发送失败',
      '待发送', '发送中', '已跳过', '尝试 6 次', 'TG not bound / bot blocked', '暂无通知记录（不代表已送达）'])
      assert(events.textContent.includes(text), 'event/notice state: ' + text);
    assert(events.textContent.includes(evil) && events.textContent.includes('only rejected excess playback') &&
      !events.querySelector('img') && !window.injected, 'event values, objects and errors are escaped plain text');
    assert(getComputedStyle(events).overflowX === 'auto', 'wide event table is scrollable on narrow screens');
    assert([...$('#ac-restrictions').querySelectorAll('input,select,button')].every(el =>
      el.getBoundingClientRect().right <= window.innerWidth), 'configuration controls fit current viewport');

    $('#ac-pattern').value = 'keep-this-UA-draft';
    $('#ac-note').value = 'keep-this-note';
    enabled('concurrency').checked = false;
    enabled('web_login').checked = true; select('web_login').value = 'pause';
    enabled('web_play').checked = true; select('web_play').value = 'disable';
    normalizeWrite = true;
    await save();
    const submitted = writes().at(-1).body;
    assert(JSON.stringify(submitted) === JSON.stringify({concurrency: {enabled: false, action: 'pause'},
      web_login: {enabled: true, action: 'pause'}, web_play: {enabled: true, action: 'disable'}}),
      'PUT sends raw config with booleans and actions, not a config wrapper');
    assert(!enabled('web_login').checked && !enabled('concurrency').checked, 'PUT response config is used');
    assert(!activeDialog(), 'pause/disable save needs no dangerous confirmation');
    assert($('#ac-pattern').value === 'keep-this-UA-draft' && $('#ac-note').value === 'keep-this-note',
      'restriction save does not reset generic-rule draft');
    assert(messages.at(-1).message.includes('已保存'), 'ordinary save reports success');

    select('web_play').value = 'pause';
    data.events = [{...data.events[0], id: 55, username: 'refreshed-member', status: 'pending'}];
    const writesBeforeRefresh = writes().length;
    await refresh();
    assert($('#ac-restriction-events').textContent.includes('refreshed-member'), 'record refresh fetches current results');
    assert(select('web_play').value === 'pause' && writes().length === writesBeforeRefresh &&
      $('#ac-pattern').value === 'keep-this-UA-draft', 'refresh is read-only and preserves all drafts');
    const beforeRefreshError = $('#ac-restriction-events').innerHTML;
    readError = 'notice read unavailable';
    await refresh();
    assert($('#ac-restriction-events').innerHTML === beforeRefreshError &&
      messages.at(-1).message.includes(readError), 'refresh error preserves records and exposes server error');
    readError = '';

    select('concurrency').value = 'delete';
    enabled('concurrency').checked = true;
    const beforeCancel = writes().length;
    const cancelled = save();
    assert(activeDialog()?.textContent.includes('将自动删除后续确认违规账号'), 'real deckConfirm describes future automatic deletion');
    assert($('#ac-restrictions-save').disabled && [...$('#ac-restrictions').querySelectorAll('input,select')].every(el => el.disabled),
      'fields and save button disabled while confirming');
    activeDialog().querySelector('.deck-dialog-btn-cancel').click();
    await cancelled;
    assert(writes().length === beforeCancel && select('concurrency').value === 'delete', 'cancel does not save or discard draft');
    assert(!$('#ac-restrictions-save').disabled && !enabled('concurrency').disabled, 'cancel restores controls');
    const confirmed = save();
    activeDialog().querySelector('.deck-dialog-btn-confirm').click();
    await confirmed;
    assert(writes().length === beforeCancel + 1 && writes().at(-1).body.concurrency.action === 'delete',
      'confirmed dangerous save writes once');
    enabled('concurrency').checked = false;
    const disabledDelete = save();
    assert(!!activeDialog(), 'even a disabled delete selection has dangerous-save confirmation');
    activeDialog().querySelector('.deck-dialog-btn-cancel').click();
    await disabledDelete;

    select('concurrency').value = 'pause';
    writeError = 'restriction write unavailable';
    const beforeWriteError = writes().length;
    await save();
    assert(writes().length === beforeWriteError + 1 && messages.at(-1).bad &&
      messages.at(-1).message.includes(writeError), 'HTTP save error reaches ordinary error toast');
    assert(select('web_play').value === 'pause' && !enabled('concurrency').checked &&
      !$('#ac-restrictions-save').disabled && !select('web_play').disabled, 'save failure preserves draft and restores controls');
    writeError = '';
    select('web_login').append(new Option('invalid', 'invalid')); select('web_login').value = 'invalid';
    const beforeInvalid = writes().length;
    await save();
    assert(writes().length === beforeInvalid && messages.at(-1).message.includes('合法'), 'invalid action is not sent');
    select('web_login').value = 'pause';

    let releaseWrite;
    writeGate = new Promise(resolve => { releaseWrite = resolve; });
    const beforeDouble = writes().length;
    const inFlight = save();
    await save();
    assert(writes().length === beforeDouble + 1 && $('#ac-restrictions-save').disabled &&
      $('#ac-restrictions-save').getAttribute('aria-busy') === 'true', 'in-flight double click cannot send a second PUT');
    const responseConfig = clone(writes().at(-1).body);
    releaseWrite(response(responseConfig));
    await inFlight;
    assert(!$('#ac-restrictions-save').disabled && !$('#ac-restrictions-save').hasAttribute('aria-busy'),
      'successful request restores disabled and busy state');
    writeGate = null;

    // Navigation during confirmation must not mutate, even if the dialog is accepted later.
    select('web_login').value = 'delete';
    const beforeNavigation = writes().length;
    const oldSaveButton = $('#ac-restrictions-save');
    const obsoleteConfirm = oldSaveButton.onclick();
    state.renderVersion++;
    activeDialog().querySelector('.deck-dialog-btn-confirm').click();
    await obsoleteConfirm;
    assert(writes().length === beforeNavigation, 'stale confirmation cannot send PUT');
    await oldSaveButton.onclick();
    assert(writes().length === beforeNavigation && !activeDialog(), 'old handler cannot act on newer page context');
    await load();

    // A completed write cannot repaint or toast into another page.
    select('concurrency').value = 'pause'; select('web_login').value = 'pause'; select('web_play').value = 'pause';
    writeGate = new Promise(resolve => { releaseWrite = resolve; });
    const staleWrite = save();
    const toastCount = messages.length;
    state.page = 'audit'; state.route = 'audit'; state.renderVersion++;
    $('#view').innerHTML = '<p id="other-page">other page stays</p>';
    releaseWrite(response(initialConfig));
    await staleWrite;
    assert($('#other-page') && messages.length === toastCount, 'stale write completion leaves other page and toast untouched');
    writeGate = null;
    await load();

    let releaseRead;
    readGate = new Promise(resolve => { releaseRead = resolve; });
    const staleRefresh = refresh();
    state.renderVersion++;
    $('#view').innerHTML = '<p id="new-context">new context stays</p>';
    releaseRead(response(data));
    await staleRefresh;
    assert($('#new-context'), 'stale event refresh cannot replace new page content');
    readGate = null;

    readError = 'restriction endpoint unavailable';
    await load();
    assert($('#view').textContent.includes(readError) && !$('#ac-restrictions-save') && $('#ac-pattern'),
      'restriction read failure is explicit, cannot save defaults, and does not hide generic rules');
    readError = '';
    const oldConfig = data.config;
    data.config = {...initialConfig, web_login: {enabled: 'false', action: 'delete'}};
    await load();
    assert(!$('#ac-restrictions-save') && $('#view').textContent.includes('数据无效'), 'invalid read config cannot be saved');
    data.config = oldConfig;
    data.events = [];
    await load();
    assert($('#ac-restriction-events').textContent.includes('暂无处罚记录'), 'empty events state is explicit');

    // The initial page read also observes the route/render-version guard.
    readGate = new Promise(resolve => { releaseRead = resolve; });
    state.renderVersion++;
    const obsoleteLoad = PAGES.access(pageContext('access'));
    state.renderVersion++;
    $('#view').innerHTML = '<p id="last-page">last page stays</p>';
    releaseRead(response(data));
    await obsoleteLoad;
    assert($('#last-page'), 'stale initial GET cannot render old access page');
    assert(requests.every(r => ['/api/access/rules', '/api/access/blocks?limit=100', '/api/access/restrictions'].includes(r.path)) &&
      requests.every(r => r.method === 'GET' || (r.path === '/api/access/restrictions' && r.method === 'PUT')),
      'test never invokes enforcement, notification, production or unrelated mutations');
    report.textContent = JSON.stringify({ok: true, checks, width: window.innerWidth});
  } catch (error) {
    report.textContent = JSON.stringify({ok: false, checks, error: error.stack || error.message});
  }
})();
