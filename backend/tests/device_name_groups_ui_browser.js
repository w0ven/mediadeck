/* Real panel scripts + real isolated API projections, no production I/O. */
(async () => {
  const report = document.getElementById('report');
  const fixture = window.__DEVICE_GROUPS;
  let current = fixture.initial;
  let checks = 0;
  const requests = [];
  const assert = (value, message) => { if (!value) throw new Error(message); checks++; };
  const tick = () => new Promise(resolve => setTimeout(resolve, 30));
  async function waitFor(predicate, label) {
    for (let i = 0; i < 100; i++) { if (predicate()) return; await tick(); }
    throw new Error(label);
  }
  window.EventSource = class { addEventListener() {} close() {} };
  api = async (path, options = {}) => {
    requests.push({path, method:options.method || 'GET'});
    if (path === fixture.block_path && options.method === 'POST') {
      current = fixture.mixed;
      return fixture.block_response;
    }
    const url = new URL(path, 'http://isolated.panel');
    if (url.pathname === '/api/members') return fixture.listing;
    if (url.pathname === '/api/members/viewer-a') return current;
    if (url.pathname === '/api/groups') return fixture.groups;
    if (url.pathname === '/api/whoami') return {user:'admin'};
    if (url.pathname === '/api/update/version') return {version:'test'};
    return {};
  };
  window.api = api;
  toast = () => {};
  const confirmations = [];
  window.deckConfirm = async text => { confirmations.push(text); return true; };
  const groups = () => [...document.querySelectorAll('.member-device-group')];
  const phone = () => groups().find(el => el.querySelector('summary').textContent.includes('iPhone'));
  const ids = () => [...document.querySelectorAll('.member-device-id')].map(el => el.textContent).sort();
  const expectedIds = fixture.initial.devices.map(row => row.device_id).sort();
  try {
    bootPanel = window.__bootPanel;
    history.replaceState(null, '', '#/members');
    bootPanel();
    await waitFor(() => document.querySelector('.member-device-count'), 'member list did not render');
    assert(document.querySelector('.member-device-count').textContent.includes('2 组'), 'list count not grouped');
    assert(document.querySelector('.member-device-count').textContent.includes('按设备名称归并'), 'list count lacks basis');
    document.querySelector('[data-act="open"]').click();
    await waitFor(() => document.querySelector('#md-renew'), 'overview missing');
    assert(document.querySelector('#member-detail').textContent.includes('2 组（按设备名称归并，非硬件唯一识别）'), 'overview count basis missing');
    document.querySelector('[data-tab="devices"]').click();
    await waitFor(() => groups().length === 2, '33 identities not grouped into two names');
    assert(groups().every(el => !el.open), 'raw IDs should start collapsed');
    assert(document.querySelector('.member-device-summary').textContent.includes('2 组'), 'detail group count mismatch');
    assert(document.querySelector('.member-device-summary').textContent.includes('33 条原始登录标识记录'), 'raw records mislabeled');
    assert(document.querySelector('#member-detail').textContent.includes('非硬件唯一识别'), 'hardware limitation missing');
    assert(document.querySelector('#member-detail').textContent.includes('两台设备同名也会合并'), 'same-name limitation missing');
    assert(document.querySelector('#member-detail').textContent.includes('Unknown device'), 'unknown fallback not explained');
    assert(phone().querySelector('summary').textContent.includes('32 条原始记录'), 'phone group lost raw identities');
    phone().querySelector('summary').click();
    assert(phone().open, 'cannot expand phone identities');
    const phoneRows = [...phone().querySelectorAll('tbody tr')];
    assert(phoneRows.length === 32, 'expanded phone group missing records');
    assert(phoneRows.every(el => el.querySelector('.member-device-id').getBoundingClientRect().height > 0), 'expanded IDs not visible');
    assert(phone().textContent.includes('SenPlayer') && phone().textContent.includes('EplayerX'), 'cross-player details lost');
    assert(ids().join() === expectedIds.join(), 'raw IDs mutated or missing');
    assert(document.querySelectorAll('[data-dev]').length === 33, 'original ID block controls missing');
    const targetId = decodeURIComponent(fixture.block_path.split('/').at(-2));
    const targetButton = [...document.querySelectorAll('[data-dev]')].find(el => el.dataset.dev === targetId);
    targetButton.click();
    await waitFor(() => phone()?.querySelector('summary').textContent.includes('31 个 ID 未封禁'), 'mixed blocked group was not refreshed');
    assert(current.member.device_count === 2, 'blocking one identity removed whole group');
    assert(confirmations.at(-1).includes(targetId) && confirmations.at(-1).includes('仅作用于此 ID'), 'block confirmation implies group action');
    const writes = requests.filter(r => r.method !== 'GET');
    assert(writes.length === 1 && writes[0].path === fixture.block_path, 'block targeted group instead of selected original ID');
    assert(ids().join() === expectedIds.join(), 'block deleted raw identities');
    current = fixture.blocked;
    document.querySelector('[data-tab="overview"]').click();
    await waitFor(() => document.querySelector('#md-renew'), 'overview reload missing');
    document.querySelector('[data-tab="devices"]').click();
    await waitFor(() => phone()?.querySelector('summary').textContent.includes('全部已封禁，不计入'), 'fully blocked group missing');
    assert(document.querySelector('.member-device-summary').textContent.includes('1 组'), 'fully blocked phone group still counted');
    assert(ids().join() === expectedIds.join(), 'fully blocked raw identities must remain expandable');
    phone().querySelector('summary').click();
    assert(phone().open && phone().querySelectorAll('[data-block="0"]').length === 32, 'unblock must stay per original ID');
    const body = document.querySelector('.hg-drawer-body');
    assert(body.scrollWidth <= body.clientWidth + 2, 'expanded identities overflow drawer');
    report.textContent = JSON.stringify({ok:true, checks});
  } catch (error) {
    report.textContent = JSON.stringify({ok:false, checks, error:error.stack || String(error)});
  }
})();
