/* Operational pages: members / groups / stats / storage / audit.
   Relies on helpers declared by app.js (api, toast, esc, PAGES, …). */

function q(value) {
  return encodeURIComponent(String(value == null ? '' : value)).replace(/'/g, '%27');
}
function uq(value) {
  try { return decodeURIComponent(String(value == null ? '' : value)); }
  catch (e) { return String(value == null ? '' : value); }
}
function daysLeftHtml(m) {
  if (m.days_remaining == null) return '<span class="muted">不限期</span>';
  const n = Number(m.days_remaining);
  const expired = m.state === 'expired' || (m.expires_at && m.expires_at * 1000 < Date.now());
  if (expired) return '<span class="danger-text">已过期</span>';
  return `<span class="${n <= 3 ? 'danger-text' : ''}">${esc(n)} 天</span>`;
}
function billingLabel(t) {
  return ({ none: '不计费', traffic: '仅流量', time: '仅时间',
    both: '时间+流量' })[t] || t || '-';
}
function fmtExpiry(ts) {
  if (!ts) return '不限期';
  const remaining = Number(ts) * 1000 - Date.now();
  const d = Math.round(remaining / 86400000);
  if (remaining < 0) return '已过期';
  if (d === 0) return '今天到期';
  return d + ' 天后';
}

/* ---------------- storage ---------------- */
PAGES.storage = async (context = pageContext('storage')) => {
  $('#view').innerHTML = pageLoading();
  const [remotes, mounts] = await Promise.all([
    api('/api/storage/remotes'), api('/api/storage/mounts'),
  ]);
  if (!context.isCurrent()) return;
  const healthy = mounts.filter((m) => m.status === 'active').length;
  const unhealthy = mounts.length - healthy;
  const usedRemotes = new Set(mounts.map((m) => m.remote));
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('☁', remotes.length, '远程账号', 'rclone remote')}
      ${stat('⛃', mounts.length, '挂载点', '全局挂载')}
      ${stat('✓', healthy, '运行中', 'active')}
      ${stat('⚠', unhealthy, '未运行', 'inactive / 异常')}
    </div>
    ${card('添加远程账号', '全局一份，节点从挂载列表勾选，不必每台机器再填',
      `<div class="card-body"><div class="toolbar">
        <input id="sr-name" aria-label="远程账号名称" placeholder="名称 mock-drive" style="width:140px">
        <input id="sr-type" aria-label="远程账号类型" placeholder="类型 drive / s3 / alias" style="width:160px">
        <input id="sr-opt" aria-label="远程账号选项 JSON" placeholder='选项 JSON 如 {"token":"***"}' style="flex:1;min-width:180px">
        <button class="btn primary" id="sr-add">添加</button>
      </div></div>`)}
    ${tableCard('远程账号', `${remotes.length} 个`, ['名称', '类型', '状态', '测试', ''],
      remotes.map((r) => `<tr>
        <td>${esc(r.name)}</td><td>${esc(r.type)}</td>
        <td>${usedRemotes.has(r.name)
          ? '<span class="tag idle">被挂载引用</span>'
          : '<span class="tag ok">已配置</span>'}</td>
        <td><button class="btn sm" onclick="testRemote('${q(r.name)}')">测试</button>
            <span class="inline-result" id="sr-res-${esc(r.name)}"></span></td>
        <td><button class="btn sm danger" onclick="deleteRemote('${q(r.name)}')">删除</button></td>
      </tr>`).join(''))}
    ${card('添加挂载点', '目标限制在面板配置的挂载根目录内',
      `<div class="card-body"><div class="toolbar">
        <input id="sm-name" aria-label="挂载名称" placeholder="名称 media-main" style="width:140px">
        <select id="sm-remote" aria-label="远程账号">${remotes.map((r) => `<option value="${esc(r.name)}">${esc(r.name)}</option>`).join('')}</select>
        <input id="sm-path" aria-label="远端路径" placeholder="远端路径 media" style="width:140px">
        <input id="sm-target" aria-label="挂载目标" placeholder="目标 media-main" style="width:140px">
        <button class="btn primary" id="sm-add">添加</button>
      </div></div>`)}
    ${tableCard('挂载点', `${mounts.length} 个`, ['名称', '远程', '目标', '状态', ''],
      mounts.map((m) => `<tr>
        <td>${esc(m.name)}</td><td>${esc(m.remote)}</td>
        <td><code>${esc(m.target)}</code></td>
        <td><span class="tag ${m.status === 'active' ? 'ok' : 'idle'}">${esc(m.status)}</span></td>
        <td class="row-actions">
          <button class="btn sm" onclick="ctlMount('${q(m.name)}','start')">启动</button>
          <button class="btn sm" onclick="ctlMount('${q(m.name)}','stop')">停止</button>
          <button class="btn sm danger" onclick="deleteMount('${q(m.name)}')">删除</button>
        </td></tr>`).join(''))}`;
  bindAsyncButton('sr-add', addRemote);
  bindAsyncButton('sm-add', addMount);
};
async function addRemote() {
  const actionContext = pageContext('storage');
  let options = {};
  const raw = ($('#sr-opt').value || '').trim();
  if (raw) {
    try { options = JSON.parse(raw); } catch (e) { return toast('选项必须是 JSON 对象', 1); }
  }
  try {
    await api('/api/storage/remotes', { method: 'POST', body: JSON.stringify({
      name: $('#sr-name').value.trim(), type: $('#sr-type').value.trim(), options }) });
    toast('已添加'); renderPage('storage', false, false, actionContext);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function testRemote(name) {
  name = uq(name);
  const el = document.getElementById('sr-res-' + name) || $(`#sr-res-${CSS.escape(name)}`);
  if (el) el.textContent = '测试中…';
  try {
    const r = await api(`/api/storage/remotes/${encodeURIComponent(name)}/test`, { method: 'POST' });
    if (el) el.innerHTML = r.ok
      ? `<span class="tag ok">${esc(r.message || 'ok')}</span>`
      : `<span class="tag bad">${esc(r.message || '失败')}</span>`;
  } catch (e) {
    if (el) el.innerHTML = `<span class="tag bad">${esc(e.message)}</span>`;
  }
}
async function deleteRemote(name) {
  const actionContext = pageContext('storage');
  name = uq(name);
  if (!(await deckConfirm(`删除远程账号 ${name}？仍被挂载引用时会被拒绝。`))) return;
  try {
    await api(`/api/storage/remotes/${encodeURIComponent(name)}`, { method: 'DELETE' });
    toast('已删除'); renderPage('storage', false, false, actionContext);
  } catch (e) { toast('无法删除: ' + e.message, 1); }
}
async function addMount() {
  const actionContext = pageContext('storage');
  try {
    await api('/api/storage/mounts', { method: 'POST', body: JSON.stringify({
      name: $('#sm-name').value.trim(), remote: $('#sm-remote').value,
      remote_path: $('#sm-path').value.trim(), target: $('#sm-target').value.trim(),
    }) });
    toast('挂载已添加'); renderPage('storage', false, false, actionContext);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function ctlMount(name, action) {
  const actionContext = pageContext('storage');
  name = uq(name);
  try {
    await api(`/api/storage/mounts/${encodeURIComponent(name)}/${action}`, { method: 'POST' });
    toast(action === 'start' ? '已启动' : '已停止'); renderPage('storage', false, false, actionContext);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function deleteMount(name) {
  const actionContext = pageContext('storage');
  name = uq(name);
  if (!(await deckConfirm(`删除挂载点 ${name}？`))) return;
  try {
    await api(`/api/storage/mounts/${encodeURIComponent(name)}`, { method: 'DELETE' });
    toast('已删除'); renderPage('storage', false, false, actionContext);
  } catch (e) { toast('失败: ' + e.message, 1); }
}

/* members page lives in members.js */

function kbpsToMBps(n) {
  n = Number(n || 0);
  if (!n) return 0;
  return Math.round((n * 125 / 1048576) * 10) / 10;
}
function mBpsToKbps(n) {
  n = Number(n || 0);
  if (!n) return 0;
  return Math.round(n * 1048576 / 125);
}
function fmtKbps(n) {
  n = Number(n || 0);
  if (!n) return '不限';
  const mb = kbpsToMBps(n);
  return (Number.isInteger(mb) ? String(mb) : mb.toFixed(1)) + ' MB/s';
}

function boolLabel(v) { return v ? '允许' : '禁止'; }
function localInputFromTs(ts) {
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function flagSelect(id, value) {
  const cur = (value === 0 || value === false) ? '0' : (value === 1 || value === true) ? '1' : '';
  return `<select id="${esc(id)}" aria-label="${esc(id)}">
    <option value="" ${cur === '' ? 'selected' : ''}>继承</option>
    <option value="1" ${cur === '1' ? 'selected' : ''}>允许</option>
    <option value="0" ${cur === '0' ? 'selected' : ''}>禁止</option>
  </select>`;
}
function ovSource(m, key, groupVal, effVal, fmt) {
  const ov = (m.overrides || {});
  const hit = (m.overridden_keys || []).includes(key) || Object.prototype.hasOwnProperty.call(ov, key);
  const shown = fmt ? fmt(effVal) : String(effVal == null ? '-' : effVal);
  const inherited = fmt ? fmt(groupVal) : String(groupVal == null ? '-' : groupVal);
  if (hit) return `<span class="tag override">已覆盖(${esc(shown)})</span>`;
  return `<span class="tag inherit">继承用户组(${esc(inherited)})</span>`;
}
const BW_PRESETS = [
  { label: '不限速', mbps: 0 },
  { label: '5 MB/s', mbps: 5 },
  { label: '10 MB/s', mbps: 10 },
  { label: '15 MB/s', mbps: 15 },
  { label: '20 MB/s', mbps: 20 },
];
function bwPresetButtons(inputId) {
  return BW_PRESETS.map((p) =>
    `<button class="btn sm" type="button" onclick="document.getElementById('${inputId}').value='${p.mbps}'">${esc(p.label)}</button>`).join(' ');
}
function overrideEditor(m, libs) {
  const ov = m.overrides || {};
  const grp = m.group || {};
  const eff = m.effective || {};
  const sel = new Set(ov.libraries || []);
  // A failed/partial library listing must not erase saved selections on save.
  const known = new Set((libs || []).map(l => l.id || l.name));
  const options = [...(libs || []), ...[...sel].filter(id => !known.has(id)).map(id => ({id, name: `未读取到的媒体库：${id}`}))];
  const libOpts = options.map((l) => {
    const id = l.id || l.name;
    return `<label style="margin-right:10px"><input type="checkbox" class="ov-lib" value="${esc(id)}" ${sel.has(id) ? 'checked' : ''}> ${esc(l.name)}</label>`;
  }).join('') || '<span class="muted">无法读取媒体库</span>';
  const num = (k) => (ov[k] != null ? ov[k] : '');
  const mode = ov.libraries_mode || 'inherit';
  const exp = ov.expires_at_override ? localInputFromTs(ov.expires_at_override) : '';
  const expMode = Object.prototype.hasOwnProperty.call(ov, 'expires_at_override') ? (ov.expires_at_override == null ? 'forever' : 'date') : 'inherit';
  const extraGib = ov.extra_traffic_bytes != null ? (ov.extra_traffic_bytes / (1024 ** 3)).toFixed(2) : '';
  return `
    <div class="ov-row"><div class="ov-label">并发</div>
      <div class="ov-src">${ovSource(m, 'max_streams', grp.max_streams || 0, eff.max_streams, (v) => v ? v + ' 路' : '不限')}</div>
      <div class="ov-controls"><input id="ov-streams" aria-label="并发覆盖" type="number" min="0" placeholder="继承" value="${esc(num('max_streams'))}" style="width:90px">
        <span class="muted">0=不限</span>
        <button class="btn sm" type="button" onclick="clearOverrideField('max_streams')">还原</button></div></div>
    <div class="ov-row"><div class="ov-label">带宽限速</div>
      <div class="ov-src">${ovSource(m, 'bandwidth_limit_kbps', grp.bandwidth_limit_kbps || 0, eff.bandwidth_limit_kbps, fmtKbps)}</div>
      <div class="ov-controls">
        <div style="margin-bottom:4px">${bwPresetButtons('ov-bandwidth')}</div>
        <input id="ov-bandwidth" aria-label="带宽覆盖" type="number" min="0" step="0.1" placeholder="继承" ${storedNumberAttrs(num('bandwidth_limit_kbps'), num('bandwidth_limit_kbps') === '' ? '' : kbpsToMBps(num('bandwidth_limit_kbps')))} style="width:110px">
        <span class="muted">MB/s，0=不限速。保存后正在播放的人会重签限速。</span>
        <button class="btn sm" type="button" onclick="clearOverrideField('bandwidth_limit_kbps')">还原</button></div></div>
    <div class="ov-row"><div class="ov-label">设备</div>
      <div class="ov-src">${ovSource(m, 'max_devices', grp.max_devices || 0, eff.max_devices, (v) => v ? v + ' 台' : '不限')}</div>
      <div class="ov-controls"><input id="ov-devices" aria-label="设备上限覆盖" type="number" min="0" placeholder="继承" value="${esc(num('max_devices'))}" style="width:90px">
        <button class="btn sm" type="button" onclick="clearOverrideField('max_devices')">还原</button></div></div>
    <div class="ov-row"><div class="ov-label">转码</div>
      <div class="ov-src">${ovSource(m, 'allow_transcode', grp.allow_transcode, eff.allow_transcode, boolLabel)}</div>
      <div class="ov-controls">${flagSelect('ov-transcode', ov.allow_transcode)}
        <button class="btn sm" type="button" onclick="clearOverrideField('allow_transcode')">还原</button></div></div>
    <div class="ov-row"><div class="ov-label">下载</div>
      <div class="ov-src">${ovSource(m, 'allow_download', grp.allow_download, eff.allow_download, boolLabel)}</div>
      <div class="ov-controls">${flagSelect('ov-download', ov.allow_download)}
        <button class="btn sm" type="button" onclick="clearOverrideField('allow_download')">还原</button></div></div>
    <div class="ov-row"><div class="ov-label">媒体库</div>
      <div class="ov-src">${ovSource(m, 'libraries_mode', 'inherit', eff.libraries_mode || 'inherit')}</div>
      <div class="ov-controls">
        <select id="ov-libmode" aria-label="媒体库覆盖方式">
          ${['inherit', 'replace', 'extend'].map((x) => `<option value="${x}" ${mode === x ? 'selected' : ''}>${esc({ inherit: '继承', replace: '替换', extend: '追加' }[x])}</option>`).join('')}
        </select>
        <button class="btn sm" type="button" onclick="clearOverrideField('libraries')">还原</button>
        <div>${libOpts}</div></div></div>
    <div class="ov-row"><div class="ov-label">到期覆盖</div>
      <div class="ov-src">${ovSource(m, 'expires_at_override', grp.duration_days ? (grp.duration_days + ' 天') : '不限期', (m.expires_at_effective !== undefined ? m.expires_at_effective : m.expires_at), (v) => typeof v === 'string' ? v : v ? fmtExpiry(v) : '不限期')}</div>
      <div class="ov-controls"><select id="ov-exp-mode" aria-label="到期覆盖模式">${[['inherit','继承原期限'],['date','指定到期'],['forever','不限期覆盖']].map(([key,label]) => `<option value="${key}" ${expMode === key ? 'selected' : ''}>${label}</option>`).join('')}</select><input id="ov-exp" aria-label="指定到期时间" type="datetime-local" ${storedNumberAttrs(ov.expires_at_override ?? '', exp)}>
        <button class="btn sm" type="button" onclick="clearOverrideField('expires_at_override')">还原</button></div></div>
    <div class="ov-row"><div class="ov-label">额外流量</div>
      <div class="ov-src">${ovSource(m, 'extra_traffic_bytes', 0, (m.overrides || {}).extra_traffic_bytes || 0, fmtBytes)}</div>
      <div class="ov-controls"><input id="ov-extra" aria-label="额外流量GiB" type="number" min="0" step="0.01" placeholder="0" ${storedNumberAttrs(ov.extra_traffic_bytes ?? '', extraGib)} style="width:110px">
        <span class="muted">GiB，叠加在本月额度上，月初清零</span>
        <button class="btn sm" type="button" onclick="clearOverrideField('extra_traffic_bytes')">还原</button></div></div>
    <div class="toolbar" style="margin-top:10px">
      <button class="btn primary" type="button" id="ov-save">保存覆盖</button>
      <button class="btn" type="button" id="ov-clear">全部还原</button>
    </div>`;
}
function collectOverridesFromForm(existing) {
  const ov = Object.assign({}, existing || {});
  const streams = ($('#ov-streams') || {}).value;
  if (streams === '' || streams == null) delete ov.max_streams;
  else ov.max_streams = parseInt(streams, 10);
  const bandwidth = ($('#ov-bandwidth') || {}).value;
  if (bandwidth === '' || bandwidth == null) delete ov.bandwidth_limit_kbps;
  else ov.bandwidth_limit_kbps = readStoredNumber($('#ov-bandwidth'), value => mBpsToKbps(parseFloat(value)));
  const devices = ($('#ov-devices') || {}).value;
  if (devices === '' || devices == null) delete ov.max_devices;
  else ov.max_devices = parseInt(devices, 10);
  const readFlag = (elId, key) => {
    const v = (($('#' + elId) || {}).value || '');
    if (v === '') delete ov[key];
    else ov[key] = v === '1' ? 1 : 0;
  };
  readFlag('ov-transcode', 'allow_transcode');
  readFlag('ov-download', 'allow_download');
  const mode = (($('#ov-libmode') || {}).value || 'inherit');
  const libs = [...document.querySelectorAll('.ov-lib:checked')].map((x) => x.value);
  if (mode === 'inherit') {
    delete ov.libraries_mode; delete ov.libraries;
  } else {
    ov.libraries_mode = mode;
    ov.libraries = libs;
  }
  const exp = (($('#ov-exp') || {}).value || '').trim();
  const expMode = ($('#ov-exp-mode') || {}).value || 'inherit';
  if (expMode === 'inherit') delete ov.expires_at_override;
  else if (expMode === 'forever') ov.expires_at_override = null;
  else {
    if (!exp) throw new Error('请选择有效的到期时间');
    const stamp = readStoredNumber($('#ov-exp'), value => Math.floor(new Date(value).getTime() / 1000));
    if (!Number.isFinite(stamp)) throw new Error('请选择有效的到期时间');
    ov.expires_at_override = stamp;
  }
  const extra = (($('#ov-extra') || {}).value || '').trim();
  if (extra === '') delete ov.extra_traffic_bytes;
  else ov.extra_traffic_bytes = readStoredNumber($('#ov-extra'), value => Math.round(parseFloat(value) * 1024 ** 3));
  return ov;
}

/* ---------------- user groups ---------------- */
PAGES.groups = async (context = pageContext('groups')) => {
  $('#view').innerHTML = pageLoading();
  const groups = await api('/api/groups');
  if (!context.isCurrent()) return;
  state.groups = groups;
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('▣', groups.length, '用户组', '计费与限制模板')}
      ${stat('☺', groups.reduce((a, g) => a + (g.member_count || 0), 0), '覆盖用户', '已分组的成员')}
    </div>
    ${groups.filter(g=>isWhitelistGroup(g.id)).map(g=>`<section class="hg-whitelist-group">${whitelistEmblem()}<div><span class="hg-eyebrow">WHITELIST · 固定系统分组</span><h3>${esc(g.name)}</h3><p>${esc(g.description || '')}</p><p>${esc(billingLabel(g.billing_mode))} · ${esc(groupLimitsText(g))} · ${esc(g.member_count || 0)} 位成员</p></div><div class="toolbar"><a class="btn" href="#/members?group_id=whitelist">查看成员</a><button class="btn" onclick="editGroup('${q(g.id)}')">调整设置</button></div></section>`).join('')}
    <details class="hg-group-new"><summary>＋ 新建用户组</summary>${card('新建用户组', '组决定计费方式和默认限制；成员可在详情里逐项覆盖', `<div class="card-body">${groupForm('new', {})}
      <div class="toolbar"><button class="btn primary" id="group-create">创建</button></div></div>`)}</details>
    ${tableCard('用户组', `${groups.length} 个`, ['名称', '计费', '默认额度', '限制', '用户', ''],
      groups.filter(g=>!isWhitelistGroup(g.id)).map((g) => `<tr>
        <td>${groupBadge(g.id,g.name)}${g.is_default ? ' <span class="tag idle">默认</span>' : ''}<div class="s muted">${esc(g.description)}</div></td>
        <td>${esc(billingLabel(g.billing_mode))}</td>
        <td>${esc(groupQuotaText(g))}</td>
        <td>${esc(groupLimitsText(g))}</td>
        <td>${esc(g.member_count || 0)}</td>
        <td class="row-actions">
          <button class="btn sm" onclick="editGroup('${q(g.id)}')">编辑</button>
          <button class="btn sm danger" onclick="deleteGroup('${q(g.id)}',${g.member_count || 0})">删除</button>
        </td></tr>`).join(''))}`;
  bindAsyncButton('group-create', () => submitGroup('new'));
};
function groupNeedsTraffic(m) { return m === 'traffic' || m === 'both'; }
function groupNeedsTime(m) { return m === 'time' || m === 'both'; }
function groupQuotaText(g) {
  const bits = [];
  if (groupNeedsTime(g.billing_mode)) bits.push(g.duration_days + ' 天');
  if (groupNeedsTraffic(g.billing_mode)) bits.push(fmtBytes(g.traffic_quota_bytes) + '/月');
  return bits.join(' · ') || '-';
}
function groupLimitsText(g) {
  const bits = [];
  bits.push(g.max_streams ? g.max_streams + ' 路' : '并发不限');
  bits.push(g.bandwidth_limit_kbps ? fmtKbps(g.bandwidth_limit_kbps) : '不限速');
  bits.push(g.max_devices ? g.max_devices + ' 设备' : '设备不限');
  bits.push(g.request_quota ? '求片 ' + g.request_quota + '/月' : '求片不限');
  bits.push(g.allow_transcode ? '转码' : '禁转码');
  bits.push(g.allow_download ? '下载' : '禁下载');
  return bits.join(' · ');
}
// Presentation rounding is not an entitlement change. Keep exact stored units
// until the operator actually edits that field.
function storedNumberAttrs(original, displayed) {
  return `value="${esc(displayed)}" data-original="${esc(original)}" data-initial="${esc(displayed)}"`;
}
function readStoredNumber(input, convert) {
  return input.dataset.original !== undefined && input.value === input.dataset.initial
    ? Number(input.dataset.original) : convert(input.value);
}
function groupForm(prefix, g) {
  const v = (k, d) => esc(g[k] != null ? g[k] : d);
  const gib = g.traffic_quota_bytes == null ? '1024' : String(g.traffic_quota_bytes / (1024 ** 3));
  const mode = g.billing_mode || 'both';
  return `
    <div class="form-row"><label for="${prefix}-id">ID</label><input id="${prefix}-id" value="${v('id', '')}" ${prefix === 'new' ? '' : 'disabled'} placeholder="standard"></div>
    <div class="form-row"><label for="${prefix}-name">名称</label><input id="${prefix}-name" value="${v('name', '')}" placeholder="普通用户"></div>
    <div class="form-row"><label for="${prefix}-description">描述</label><input id="${prefix}-description" value="${v('description', '')}"></div>
    <div class="form-row"><label for="${prefix}-default">默认组</label><input id="${prefix}-default" type="checkbox" ${g.is_default ? 'checked' : ''}>
      <span class="muted">新纳入的账号进这个组</span></div>
    <div class="help">计费方式。时间=有到期日；流量=每月 1 日重置额度；两者可同时启用。</div>
    <div class="form-row"><label for="${prefix}-billing">计费</label>
      <select id="${prefix}-billing">
        ${[['both', '时间+流量'], ['traffic', '仅流量'], ['time', '仅时间'], ['none', '不计费']].map(([val, label]) =>
          `<option value="${val}" ${mode === val ? 'selected' : ''}>${label}</option>`).join('')}
      </select></div>
    <div class="form-row"><label for="${prefix}-days">默认时长</label><input id="${prefix}-days" type="number" min="0" value="${v('duration_days', 30)}" style="width:100px"><span class="muted">天，计时间的组必填</span></div>
    <div class="form-row"><label for="${prefix}-gib">月流量</label><input id="${prefix}-gib" type="number" min="0" step="any" ${storedNumberAttrs(g.traffic_quota_bytes ?? 1024 ** 4, gib)} style="width:110px"><span class="muted">GiB，计流量的组必填</span></div>
    <div class="help">默认限制。0 = 不限。成员详情里可以逐个覆盖。</div>
    <div class="form-row"><label for="${prefix}-bandwidth">带宽限速</label>
      <div><div style="margin-bottom:4px">${bwPresetButtons(prefix + '-bandwidth')}</div>
      <input id="${prefix}-bandwidth" type="number" min="0" step="0.1" ${storedNumberAttrs(g.bandwidth_limit_kbps || 0, kbpsToMBps(g.bandwidth_limit_kbps || 0))} style="width:110px">
      <span class="muted">MB/s，0 = 不限速。保存后该组未覆盖成员会重签限速。</span></div></div>
    <div class="form-row"><label for="${prefix}-streams">并发</label><input id="${prefix}-streams" type="number" min="0" value="${v('max_streams', 2)}" style="width:90px"><span class="muted">路，0 = 不限</span></div>
    <div class="form-row"><label for="${prefix}-devices">设备</label><input id="${prefix}-devices" type="number" min="0" value="${v('max_devices', 3)}" style="width:90px"><span class="muted">台，0 = 不限</span></div>
    <div class="form-row"><label for="${prefix}-requests">每月求片</label><input id="${prefix}-requests" type="number" min="0" value="${v('request_quota', 3)}" style="width:90px"><span class="muted">次/月，0 = 不限；被拒绝的求片也算一次</span></div>
    <div class="form-row"><label>权限</label>
      <label><input id="${prefix}-transcode" type="checkbox" ${g.allow_transcode == null || g.allow_transcode ? 'checked' : ''}> 转码</label>
      <label><input id="${prefix}-download" type="checkbox" ${g.allow_download ? 'checked' : ''}> 下载</label></div>`;
}
function groupPayload(prefix) {
  const gib = parseFloat($(`#${prefix}-gib`).value) || 0;
  return {
    id: $(`#${prefix}-id`).value.trim(),
    name: $(`#${prefix}-name`).value.trim(),
    description: $(`#${prefix}-description`).value.trim(),
    is_default: $(`#${prefix}-default`).checked,
    billing_mode: $(`#${prefix}-billing`).value,
    duration_days: parseInt($(`#${prefix}-days`).value, 10) || 0,
    traffic_quota_bytes: readStoredNumber($(`#${prefix}-gib`), () => Math.round(gib * 1024 ** 3)),
    bandwidth_limit_kbps: readStoredNumber($(`#${prefix}-bandwidth`), value => mBpsToKbps(parseFloat(value) || 0)),
    max_streams: parseInt($(`#${prefix}-streams`).value, 10) || 0,
    max_devices: parseInt($(`#${prefix}-devices`).value, 10) || 0,
    request_quota: parseInt($(`#${prefix}-requests`).value, 10) || 0,
    allow_transcode: $(`#${prefix}-transcode`).checked,
    allow_download: $(`#${prefix}-download`).checked,
  };
}
async function submitGroup(prefix, existingId) {
  const actionContext = pageContext('groups');
  const context = pageContext('groups');
  const modal = existingId ? $('#modal-root') : null;
  const payload = groupPayload(prefix);
  try {
    if (existingId) {
      if (!(await deckConfirm('保存后会立即更新该组未单独覆盖限速的成员，正在播放的人会重签限速。'))) return;
      await api(`/api/groups/${encodeURIComponent(existingId)}`, { method: 'PUT', body: JSON.stringify(payload) });
      toast('已保存');
      if (!context.isCurrent() || !modal.isConnected) return;
      closeModal();
    } else {
      await api('/api/groups', { method: 'POST', body: JSON.stringify(payload) });
      toast('已创建');
    }
    if (context.isCurrent()) renderPage('groups', false, false, actionContext);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
function editGroup(id) {
  id = uq(id);
  const g = (state.groups || []).find((x) => x.id === id);
  if (!g) return;
  openModal('编辑用户组 ' + g.name, `${groupForm('edit', g)}
    <div class="toolbar">
      <button class="btn" id="group-preview">当前策略（不含草稿）</button>
      <button class="btn primary" id="group-save">保存</button>
    </div>`, { wide: true });
  bindAsyncButton('group-save', () => submitGroup('edit', id));
  const modal = $('#modal-root');
  const preview = document.createElement('div');
  modal.querySelector('.modal-body').append(preview);
  bindAsyncButton('group-preview', async () => {
    const r = await api('/api/enforcement/preview');
    if (!modal.isConnected) return;
    preview.innerHTML = `<p class="help">当前已保存策略的下发预览；不包含上方未保存的用户组修改，不会写入服务。</p>${tableCard('当前将变更', '', ['用户', '状态', '字段'],
      (r.changes || []).map(c => `<tr><td>${esc(c.username)}</td><td>${esc(c.state)}</td><td>${esc(Object.keys(c.changes || {}).join(', '))}</td></tr>`).join(''))}`;
  });
}
async function deleteGroup(id, count) {
  const actionContext = pageContext('groups');
  id = uq(id);
  if (isWhitelistGroup(id)) return toast('白名单是固定系统分组，不能删除', 1);
  if (count) return toast(`仍有 ${count} 个用户在该组，请先迁移`, 1);
  if (!(await deckConfirm(`删除用户组 ${id}？`))) return;
  try {
    await api(`/api/groups/${encodeURIComponent(id)}`, { method: 'DELETE' });
    toast('已删除'); renderPage('groups', false, false, actionContext);
  } catch (e) { toast('无法删除: ' + e.message, 1); }
}

/* ---------------- stats ---------------- */
PAGES.stats = async (context = pageContext('stats')) => {
  const days = state.statsDays || 30;
  $('#view').innerHTML = pageLoading();
  const [overview, daily, users, titles, clients, methods] = await Promise.all([
    api(`/api/stats/overview?days=${days}`),
    api(`/api/stats/daily?days=${days}`),
    api(`/api/stats/top-users?days=${days}`),
    api(`/api/stats/top-titles?days=${days}`),
    api(`/api/stats/clients?days=${days}`),
    api(`/api/stats/play-methods?days=${days}`),
  ]);
  if (!context.isCurrent()) return;
  const trafficNow = (overview.traffic || {}).month_bytes;
  const hoursNow = (overview.traffic || {}).window_hours || 0;
  const playsNow = (overview.playback || {}).window_plays || 0;
  const activeNow = (overview.members || {}).active || 0;
  const usersByHours = (users || []).slice().sort((a, b) => (b.hours || 0) - (a.hours || 0));
  $('#view').innerHTML = `
    <div class="filter-bar">
      ${[7, 30, 90].map((n) => `<button class="btn ${n === days ? 'primary' : ''}" onclick="statsRange(${n})">${n} 天</button>`).join('')}
    </div>
    <div class="stat-grid">
      ${stat('⇅', actualBytes(trafficNow), '本月实测流量', `${(overview.traffic || {}).period || ''} · UTC 自然月 · 不随观看窗口切换`)}
      ${stat('▶', hoursNow + ' 小时', '已记录观看时长', overview.traffic.window_incomplete ? '已确认部分；跨界旧记录无法拆分' : `近 ${days} 天 · 实际采样`)}
      ${stat('☺', activeNow, '活跃用户', '当前正常成员')}
      ${stat('▣', playsNow, '播放次数', `近 ${days} 天已结束会话`)}
    </div>
    ${card('每日观看', 'UTC 自然日；跨界旧记录只展示已确认部分，不估算拆分', `<div class="chart-wrap">${trendChart(daily)}<div class="chart-tip" id="chart-tip"></div></div>
      <div class="chart-legend"><span><i class="swatch" style="background:#12b76a"></i>观看时长</span></div>`)}
    ${card('转码占比', '直通越多，CPU 越省', playMethodPanel(methods, overview))}
    <div class="grid-2">
      ${tableCard('热门内容', `${titles.length} 条`, ['内容', '播放', '分钟'],
        titles.map((t) => `<tr><td>${esc(t.title || '(未命名)')}</td><td>${esc(t.plays)}</td>
          <td>${esc(Math.round((t.hours || 0) * 60))}</td></tr>`).join(''))}
      ${tableCard('用户观看排行', `近 ${days} 天观看 · 流量为本月实测`, ['用户', '已记录观看', '本月流量'],
        usersByHours.map(u => `<tr><td>${esc(u.username)}</td><td>${esc(fmtWatchSeconds(u.seconds))}${u.incomplete ? ' · 部分历史无法拆分' : ''}</td><td>${actualBytes(u.bytes)}</td></tr>`).join(''))}
    </div>
    <div class="grid-2">
      ${card('客户端分布', '', barList((clients || []).map((c) => ({ label: c.client, pct: c.percent, extra: c.plays + ' 次' }))))}
    </div>`;
  bindChartHover(daily);
};
function statsRange(days) { state.statsDays = days; renderPage('stats'); }
function earlier(prev, now, group, key) {
  if (!prev || !prev[group] || !now || !now[group]) return null;
  return Math.max(0, Number(prev[group][key] || 0) - Number(now[group][key] || 0));
}
function deltaText(now, prev) {
  /* stat() escapes `sub`, so this must be plain text. */
  if (prev == null) return '—';
  if (!prev && !now) return '持平';
  if (!prev) return '↑ 新数据';
  const pct = Math.round((now - prev) / prev * 100);
  const arrow = pct > 0 ? '↑' : pct < 0 ? '↓' : '→';
  return `${arrow} ${Math.abs(pct)}%`;
}
function playMethodPanel(methods, overview) {
  const ratio = (overview.playback || {}).direct_ratio;
  const t = methods || {};
  if (!t.total) {
    return '<div class="empty">还没有播放记录</div>';
  }
  return `<div class="card-body">
    <div class="stat-grid">
      ${stat('▷', (t.direct_ratio == null ? (ratio == null ? '-' : ratio + '%') : t.direct_ratio + '%'), '直通占比', `${t.direct || 0} 次`)}
      ${stat('⚙', (t.transcode_ratio == null ? '-' : t.transcode_ratio + '%'), '转码占比', `${t.transcode || 0} 次`)}
    </div>
    ${barList((t.methods || []).map((m) => ({
      label: m.method, pct: t.total ? Math.round(m.plays / t.total * 100) : 0, extra: m.plays + ' 次',
    })))}
  </div>`;
}
function barList(items) {
  if (!items.length) return '<div class="empty">暂无数据</div>';
  return items.map((it) => `<div class="hbar"><div class="lab" title="${esc(it.label)}">${esc(it.label)}</div>
    <div class="track"><i style="width:${Math.max(0, Math.min(100, it.pct || 0))}%"></i></div>
    <div class="pct">${esc(it.pct || 0)}% ${esc(it.extra || '')}</div></div>`).join('');
}
function trendChart(daily) {
  const w = 640; const h = 200; const pad = 28;
  if (!daily.length) {
    return `<svg viewBox="0 0 ${w} ${h}" class="chart-svg"><text x="20" y="100" fill="#8a93a3">暂无数据</text></svg>`;
  }
  const maxB = Math.max(...daily.map((d) => d.bytes || 0), 1);
  const maxH = Math.max(...daily.map((d) => d.hours || 0), 0.01);
  const innerW = w - pad * 2; const innerH = h - pad * 2;
  const x = (i) => pad + (daily.length === 1 ? innerW / 2 : i * innerW / (daily.length - 1));
  const yB = (v) => pad + innerH - (v / maxB) * innerH;
  const yH = (v) => pad + innerH - (v / maxH) * innerH;
  const line = (key, yn, color) => {
    const pts = daily.map((d, i) => `${x(i).toFixed(1)},${yn(d[key] || 0).toFixed(1)}`).join(' ');
    const area = `${pad},${pad + innerH} ${pts} ${x(daily.length - 1).toFixed(1)},${pad + innerH}`;
    return `<polygon fill="${color}" fill-opacity="0.12" points="${area}"/>
      <polyline fill="none" stroke="${color}" stroke-width="2" points="${pts}"/>`;
  };
  const hits = daily.map((d, i) => `<circle class="chart-hit" data-i="${i}" cx="${x(i).toFixed(1)}" cy="${yH(d.hours || 0).toFixed(1)}" r="8" fill="transparent"/>`).join('');
  return `<svg viewBox="0 0 ${w} ${h}" class="chart-svg" id="trend-svg">${line('hours', yH, '#12b76a')}${hits}</svg>`;
}
function bindChartHover(daily) {
  const svg = $('#trend-svg'); const tip = $('#chart-tip');
  if (!svg || !tip) return;
  svg.querySelectorAll('.chart-hit').forEach((el) => {
    el.addEventListener('mousemove', (ev) => {
      const d = daily[Number(el.dataset.i)];
      if (!d) return;
      tip.style.display = 'block';
      tip.style.left = (ev.offsetX + 12) + 'px';
      tip.style.top = (ev.offsetY + 8) + 'px';
      tip.textContent = `${d.day} · ${d.hours} 小时${d.incomplete ? ' · 部分跨界历史无法拆分' : ''}`;
    });
    el.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
  });
}

/* ---------------- audit ---------------- */
PAGES.audit = async (context = pageContext('audit')) => {
  $('#view').innerHTML = pageLoading();
  state.auditOffset = 0;
  state.auditLimit = 50;
  await loadAudit(context);
};
function auditQuery() {
  const qs = new URLSearchParams();
  qs.set('limit', String(state.auditLimit || 50));
  qs.set('offset', String(state.auditOffset || 0));
  const actor = (($('#au-actor') || {}).value || '').trim();
  const action = (($('#au-action') || {}).value || '').trim();
  const subject = (($('#au-subject') || {}).value || '').trim();
  if (actor) qs.set('actor', actor);
  if (action) qs.set('action', action);
  if (subject) qs.set('subject', subject);
  return qs;
}
async function loadAudit(context = pageContext('audit')) {
  const generation = state.auditGeneration = (state.auditGeneration || 0) + 1;
  const current = () => context.isCurrent() && generation === state.auditGeneration;
  try {
    const data = await api('/api/audit?' + auditQuery().toString());
    if (!current()) return;
    const items = Array.isArray(data) ? data : (data.items || []);
    const total = Array.isArray(data) ? items.length : (data.total || 0);
    const limit = Array.isArray(data) ? items.length : (data.limit || 50);
    const offset = Array.isArray(data) ? 0 : (data.offset || 0);
    state.audit = data;
    const page = Math.floor(offset / Math.max(limit, 1)) + 1;
    const pages = Math.max(1, Math.ceil(total / Math.max(limit, 1)));
    $('#view').innerHTML = `
      <div class="filter-bar">
        <input id="au-actor" aria-label="操作者筛选" placeholder="操作者" style="width:120px" value="${esc((($('#au-actor') || {}).value || ''))}">
        <input id="au-action" aria-label="动作筛选" placeholder="动作" style="width:140px" value="${esc((($('#au-action') || {}).value || ''))}">
        <input id="au-subject" aria-label="对象筛选" placeholder="对象" style="width:140px" value="${esc((($('#au-subject') || {}).value || ''))}">
        <button class="btn" id="au-go">筛选</button>
      </div>
      <div class="pager">
        <span>共 ${esc(total)} 条 · 第 ${esc(page)}/${esc(pages)} 页</span>
        <button class="btn sm" id="au-prev" ${offset <= 0 ? 'disabled' : ''}>上一页</button>
        <button class="btn sm" id="au-next" ${offset + limit >= total ? 'disabled' : ''}>下一页</button>
      </div>
      ${tableCard('审计日志', `${items.length} 条`, ['时间', '操作者', '动作', '对象', '详情', '结果'],
        auditRows(items))}`;
    $('#au-go').onclick = () => { state.auditOffset = 0; loadAudit(); };
    $('#au-prev').onclick = () => {
      state.auditOffset = Math.max(0, offset - limit); loadAudit();
    };
    $('#au-next').onclick = () => {
      state.auditOffset = offset + limit; loadAudit();
    };
  } catch (e) {
    if (!current()) return;
    $('#view').innerHTML = pageError(e);
    if ($('#retry-page')) $('#retry-page').onclick = () => loadAudit();
  }
}
function auditRows(rows) {
  return rows.map((a) => `<tr>
    <td>${esc(fmtAgeTs(a.ts))}</td><td>${esc(a.actor)}</td><td>${esc(a.action)}</td>
    <td>${esc(a.subject)}</td><td>${esc(a.detail)}</td>
    <td>${a.ok ? '<span class="tag ok">成功</span>' : '<span class="tag bad">失败</span>'}</td>
  </tr>`).join('');
}

/* bootPanel runs from members.js after the members page is registered. */

/* ---------- Telegram ----------
   Grouped as their own nav section: the bot is a second front door with its
   own settings, its own approval queue and its own audit, and scattering those
   across "settings" and "members" made each of them hard to find. */

PAGES.tgbot = async (context = pageContext('tgbot')) => {
  $('#view').innerHTML = pageLoading();
  const tg = await api('/api/settings/telegram').catch(() => null);
  if (!context.isCurrent()) return;
  if (!tg) { $('#view').innerHTML = pageError('无法读取 Telegram 配置'); return; }
  if (!context.isCurrent()) return;
  const st = tg.status || {};
  const running = st.running && tg.enabled;
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('✈', running ? '运行中' : (tg.bot_token_set ? '已停止' : '未配置'), '机器人',
        st.last_error ? `最近错误：${st.last_error}` : (tg.bot_token_set ? tg.bot_token_masked : '尚未填写 Token'))}
      ${stat('🆕', tg.registration_open ? '开放' : '关闭', '注册',
        [tg.allow_admin_grant ? '预授权' : '', tg.allow_invite ? '邀请码' : '',
          tg.allow_redeem ? '卡密' : ''].filter(Boolean).join(' / ') || '全部通道已关闭')}
      ${stat('☺', tg.max_users ? `上限 ${tg.max_users}` : '不限', '名额',
        (tg.membership_rules?.gate_enabled && (tg.membership_rules.targets||[]).some(t=>t.enabled))
          ? '需加入关联群组/频道' : '无群组要求')}
      ${stat('⚡', '任务中心', '定时推送', '排行与到期提醒已迁至自动化')}
    </div>
    ${card('机器人对接', 'Token 仅保存在服务端，不会回传浏览器',
      `<div class="card-body">
        <div class="form-row"><label for="tg-token">Bot Token</label>
          <input id="tg-token" type="password" autocomplete="new-password"
            placeholder="${tg.bot_token_set ? esc(tg.bot_token_masked) + '（留空则不修改）' : '向 @BotFather 申请后粘贴'}"></div>
        <div class="form-row"><label for="tg-enabled">启用机器人</label>
          <input id="tg-enabled" type="checkbox" ${tg.enabled ? 'checked' : ''}>
          <span class="muted">关闭后停止收发消息，配置保留</span></div>
        <div class="form-row"><label for="tg-embyurl">Emby 地址</label>
          <input id="tg-embyurl" value="${esc(tg.emby_public_url || '')}" placeholder="https://emby.example.com">
          <span class="muted">随账号一起发给新成员</span></div>
        <div class="form-row"><label for="tg-logo">首页 Logo</label>
          <input id="tg-logo" type="url" value="${esc(tg.menu_logo_url || '')}" placeholder="https://example.com/logo.png" autocomplete="off">
          <button class="btn" id="tg-logo-preview">预览</button>
          <button class="btn" id="tg-logo-clear">关闭图片</button></div>
        <div class="help">填写公开 HTTPS 图片直链；图片仅用于 Bot 首页，留空保持纯文字。Telegram 无法加载时自动回退文字，求片等流程仍使用单消息。</div>
        <div id="tg-logo-preview-box" class="card-body" hidden>
          <img id="tg-logo-image" alt="Bot 首页 Logo 预览" referrerpolicy="no-referrer" style="max-width:280px;max-height:160px;object-fit:contain;border-radius:12px">
          <div id="tg-logo-hint" class="muted" aria-live="polite"></div>
        </div>
        <div class="toolbar">
          <button class="btn" id="tg-test">测试连接</button>
          <button class="btn primary" id="tg-save">保存</button>
          <span id="tg-result" class="muted"></span>
        </div>
      </div>`)}
    ${card('会员播放线路', 'Bot「播放线路」给会员看的入口；不是内部节点名',
      `<div class="card-body">
        <p class="help">按会员实际填写的地址排列。全部留空则沿用上面的 Emby 地址，并继续显示节点水位。名称和地址会按纯文本发出，不能写 HTML。</p>
        <textarea id="tg-lines" hidden aria-label="播放线路配置">${esc(JSON.stringify(tg.playback_lines || []))}</textarea>
        <div class="line-layout">
          <div>
            <div id="tg-line-rows" class="line-rows"></div>
            <div class="toolbar"><button type="button" class="btn" id="tg-line-add">＋ 添加线路</button></div>
            <div class="form-row"><label for="tg-lines-note">页脚说明</label>
              <textarea id="tg-lines-note" rows="4" maxlength="1500" placeholder="例如：主线路建议挂梯；优选请按自己的运营商选择">${esc(tg.playback_lines_note || '')}</textarea></div>
            <div class="form-row"><label for="tg-lines-load">显示节点水位</label>
              <input id="tg-lines-load" type="checkbox" ${tg.playback_lines_show_load !== false ? 'checked' : ''}>
              <span class="muted">水位是内部调度状态，可选附在地址后面</span></div>
            <button type="button" class="btn primary" id="tg-save-lines">保存播放线路</button>
          </div>
          <aside class="line-preview" aria-live="polite">
            <div class="line-preview-kicker">Bot 预览</div>
            <div id="tg-line-preview" class="line-preview-body"></div>
          </aside>
        </div>
      </div>`)}
    ${card('注册开户', '注册需要凭证：预授权、邀请码或卡密，三选一',
      `<div class="card-body">
        <div class="form-row"><label for="tg-ch-admin">管理员预授权</label>
          <input id="tg-ch-admin" type="checkbox" ${tg.allow_admin_grant ? 'checked' : ''}>
          <span class="muted">名单在「邀请与授权」页维护</span></div>
        <div class="form-row"><label for="tg-ch-invite">邀请码</label>
          <input id="tg-ch-invite" type="checkbox" ${tg.allow_invite ? 'checked' : ''}>
          <span class="muted">老用户用自己的名额生成</span></div>
        <div class="form-row"><label for="tg-ch-redeem">卡密</label>
          <input id="tg-ch-redeem" type="checkbox" ${tg.allow_redeem ? 'checked' : ''}>
          <span class="muted">在「卡密管理」页批量生成</span></div>
        <div class="help">三个通道全部关闭 = 停止注册。卡密自带套餐和天数，下面的赠送天数只对预授权和邀请码生效。</div>
        <div class="form-row"><label for="tg-regdays">赠送天数</label>
          <input id="tg-regdays" type="number" min="0" max="3650" value="${esc(tg.register_days)}" style="width:100px">
          <span class="muted">0 = 不限期</span></div>
        <div class="form-row"><label for="tg-max">注册名额</label>
          <input id="tg-max" type="number" min="0" value="${esc(tg.max_users)}" style="width:100px">
          <span class="muted">0 = 不限；名额是防止链接外泄后被刷爆的唯一闸门</span></div>
        <div class="form-row"><label for="tg-group">默认用户组</label>
          <input id="tg-group" value="${esc(tg.default_group_id || '')}" placeholder="留空使用系统默认组"></div>
        <p class="help">加入群组/频道的要求只在「群组与频道」里配置，这里不再重复填写。</p>
        <div class="toolbar"><button class="btn primary" id="tg-save2">保存</button></div>
      </div>`)}
    ${card('通知与排行', '这两件事现在是定时任务',
      `<div class="card-body">
        <div class="muted" style="line-height:1.6">
          <b>到期提醒</b>与<b>排行推送</b>已迁至「自动化 → 任务中心」，
          在那里配置目标群组、时间和开关。
          放在两个页面各有一个开关，先后保存会互相覆盖，也会让同一条消息发两遍。
        </div>
        <div class="toolbar" style="margin-top:12px">
          <button class="btn" onclick="go('automation')">前往任务中心</button>
          <button class="btn" id="tg-sendrank">立即发送一次排行</button>
          <span id="tg-rankresult" class="muted"></span>
        </div>
      </div>`)}`;
  bindAsyncButton('tg-sendrank', sendRankingsNow);
  $('#tg-logo-preview').onclick = () => {
    const box = $('#tg-logo-preview-box'), img = $('#tg-logo-image'), hint = $('#tg-logo-hint');
    try {
      const url = new URL($('#tg-logo').value.trim());
      if (url.protocol !== 'https:' || url.username || url.password || url.hash) throw new Error('请填写公开 HTTPS 图片直链');
      box.hidden = false; hint.textContent = '正在加载预览…'; img.hidden = false;
      img.onload = () => { hint.textContent = '首页图片预览 · 保存后生效'; };
      img.onerror = () => { img.hidden = true; hint.textContent = '图片未能加载，请检查直链；Bot 会回退文字菜单。'; };
      img.src = url.href;
    } catch (e) { box.hidden = false; img.hidden = true; hint.textContent = '请填写有效的公开 HTTPS 图片直链。'; }
  };
  $('#tg-logo-clear').onclick = () => { $('#tg-logo').value = ''; $('#tg-logo-preview-box').hidden = true; $('#tg-logo-image').removeAttribute('src'); updateDirtyBadges(); };
  initTelegramSettings(tg);
  if (tg.menu_logo_url) $('#tg-logo-preview').click();
};

/* An empty token box means "keep the stored one", never "clear it": the
   sentinel is what tells the server which of the two was meant. */
function telegramPagePayload() {
  const typed = ($('#tg-token') || {}).value || '';
  const num = (id, dflt) => {
    const el = $('#' + id);
    return el ? Number(el.value || dflt) : dflt;
  };
  const str = (id) => (($('#' + id) || {}).value || '').trim();
  const flag = (id) => (($('#' + id) || {}).checked) || false;
  return {
    bot_token: typed.trim() || SECRET_KEEP,
    enabled: flag('tg-enabled'),
    emby_public_url: str('tg-embyurl'),
    menu_logo_url: str('tg-logo'),
    allow_admin_grant: flag('tg-ch-admin'),
    allow_invite: flag('tg-ch-invite'),
    allow_redeem: flag('tg-ch-redeem'),
    register_days: num('tg-regdays', 30),
    max_users: num('tg-max', 0),
    default_group_id: str('tg-group'),
  };
}
async function sendRankingsNow() {
  const el = $('#tg-rankresult');
  el.textContent = '发送中…';
  try {
    const r = await api('/api/telegram/rankings/send', {
      method: 'POST', body: JSON.stringify({ days: 1 }) });
    el.innerHTML = r.sent
      ? '<span class="tag ok">已发送</span>'
      : '<span class="tag bad">发送失败</span>';
  } catch (e) { el.innerHTML = `<span class="tag bad">${esc(e.message)}</span>`; }
}

/* ---------- redeem codes ----------
   A card is a bearer credential: whoever reads it can spend it. The table
   masks them and reveals one on demand, while the generation result and the
   CSV export show them in full -- those are the operator handing cards out,
   which is the entire point of minting them. */
PAGES.redeem = async (context = pageContext('redeem')) => {
  $('#view').innerHTML = pageLoading();
  const [listing, groups] = await Promise.all([
    api('/api/redeem'), api('/api/groups')]);
  if (!context.isCurrent()) return;
  state.redeemListing = listing;
  const codes = listing.codes || [];
  const st = listing.stats || {};
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('🎟', st.unused || 0, '未使用', '可以发出去的')}
      ${stat('✓', st.used || 0, '已使用', '已经换成账号')}
      ${stat('⊘', st.revoked || 0, '已作废', '不再可用')}
    </div>
    ${card('生成卡密', '每张卡自带套餐和天数，注册时一次性核销',
      `<div class="card-body">
        <div class="form-row"><label for="rd-group">套餐</label>
          <select id="rd-group">${groups.map((g) => `<option value="${esc(g.id)}" ${g.is_default ? 'selected' : ''}>${esc(g.name)}</option>`).join('')}</select></div>
        <div class="form-row"><label for="rd-days">天数</label>
          <input id="rd-days" type="number" min="0" max="3650" value="30" style="width:110px">
          <span class="muted">0 = 不限期</span></div>
        <div class="form-row"><label for="rd-count">数量</label>
          <input id="rd-count" type="number" min="1" max="500" value="10" style="width:110px">
          <span class="muted">一次最多 500 张</span></div>
        <div class="form-row"><label for="rd-batch">批次名</label>
          <input id="rd-batch" placeholder="留空自动按时间生成"></div>
        <div class="form-row"><label for="rd-note">备注</label>
          <input id="rd-note" placeholder="给自己看的说明，例如「双十一活动」"></div>
        <div class="toolbar"><button class="btn primary" id="rd-make">生成</button></div>
        <div id="rd-result"></div>
      </div>`)}
    <div class="filter-bar">
      <select id="rd-f-status" aria-label="卡密状态筛选">
        <option value="">全部状态</option>
        <option value="unused">未使用</option><option value="used">已使用</option>
        <option value="revoked">已作废</option>
      </select>
      <select id="rd-f-batch" aria-label="卡密批次筛选"><option value="">全部批次</option>${
  (listing.batches || []).map((b) => `<option value="${esc(b)}">${esc(b)}</option>`).join('')}</select>
      <button class="btn primary" id="rd-filter">筛选</button>
      <button class="btn" id="rd-export">导出 CSV</button>
    </div>
    <div id="redeem-table">${redeemTable(codes)}</div>`;
  bindAsyncButton('rd-make', generateRedeem);
  $('#rd-filter').onclick = () => renderRedeemFiltered();
  $('#rd-export').onclick = exportRedeem;
};
function redeemTable(codes) {
  const statusLabel = { unused: ['ok', '未使用'], used: ['idle', '已使用'],
    revoked: ['bad', '已作废'] };
  return tableCard('卡密', `${codes.length} 张`,
    ['卡密', '套餐', '天数', '状态', '批次', '使用者', '使用时间', ''],
    codes.map((c) => {
      const [cls, label] = statusLabel[c.status] || ['idle', c.status];
      return `<tr>
        <td><button type="button" class="code-cell linkish" title="点击复制完整卡密"
          onclick="copyRedeem('${q(c.code)}', this)">${esc(c.masked)}</button></td>
        <td>${esc(c.group_name || c.group_id)}</td>
        <td>${c.days ? esc(c.days) + ' 天' : '不限期'}</td>
        <td><span class="tag ${cls}">${esc(label)}</span></td>
        <td>${esc(c.batch || '—')}</td>
        <td>${esc(c.used_by || '—')}</td>
        <td>${c.used_at ? esc(fmtAgeTs(c.used_at)) : '<span class="muted">—</span>'}</td>
        <td class="icon-actions">${c.status === 'unused'
    ? `<button class="btn sm danger" aria-label="作废" title="作废" onclick="revokeRedeem('${q(c.code)}')">⊘</button>`
    : ''}</td></tr>`;
    }).join(''));
}
function redeemFilterQuery() {
  const params = new URLSearchParams();
  const stv = (($('#rd-f-status') || {}).value || '');
  const bv = (($('#rd-f-batch') || {}).value || '');
  if (stv) params.set('status', stv);
  if (bv) params.set('batch', bv);
  return params.toString();
}
async function renderRedeemFiltered() {
  const qs = redeemFilterQuery();
  const host = $('#redeem-table');
  const generation = state.redeemGeneration = (state.redeemGeneration || 0) + 1;
  try {
    const listing = await api('/api/redeem' + (qs ? '?' + qs : ''));
    if (!host?.isConnected || generation !== state.redeemGeneration) return;
    state.redeemListing = listing;
    host.innerHTML = redeemTable(listing.codes || []);
  } catch (e) { if (host?.isConnected) toast('筛选失败: ' + e.message, 1); }
}
async function generateRedeem() {
  const body = {
    group_id: ($('#rd-group') || {}).value || '',
    days: Number(($('#rd-days') || {}).value || 0),
    count: Number(($('#rd-count') || {}).value || 0),
    batch: (($('#rd-batch') || {}).value || '').trim(),
    note: (($('#rd-note') || {}).value || '').trim(),
  };
  try {
    const r = await api('/api/redeem/generate', {
      method: 'POST', body: JSON.stringify(body) });
    const values = (r.codes || []).map((c) => c.code);
    state.lastRedeemBatch = values;
    const box = $('#rd-result');
    if (box) {
      box.innerHTML = `
        <div class="help" style="margin-top:14px">
          已生成 <b>${values.length}</b> 张（批次 ${esc((r.codes[0] || {}).batch || '')}）。
          <b>这是唯一一次完整显示</b>，列表里只会看到掩码。
        </div>
        <div class="code-dump" id="rd-dump">${esc(values.join('\n'))}</div>
        <div class="toolbar" style="margin-top:10px">
          <button class="btn" id="rd-copy">复制全部</button>
          <button class="btn" id="rd-dl">下载 CSV</button>
        </div>`;
      $('#rd-copy').onclick = () => copyText(values.join('\n'), `已复制 ${values.length} 张`);
      $('#rd-dl').onclick = () => downloadRedeemCsv((r.codes[0] || {}).batch || '');
    }
    toast(`已生成 ${values.length} 张`);
  } catch (e) { toast('生成失败: ' + e.message, 1); }
}
async function copyText(text, okMsg) {
  try {
    await navigator.clipboard.writeText(text);
    toast(okMsg || '已复制');
    return true;
  } catch (e) {
    // Clipboard access is denied outside a secure context, which is exactly
    // where a self-hosted panel often runs. Say so instead of failing mutely.
    toast('复制失败，请手动选择文本', 1);
    return false;
  }
}
function copyRedeem(value, el) {
  value = uq(value);
  copyText(value, '已复制完整卡密').then((ok) => {
    if (ok && el) {
      const was = el.textContent;
      el.textContent = value;
      setTimeout(() => { el.textContent = was; }, 4000);
    }
  });
}
function downloadRedeemCsv(batch) {
  const qs = batch ? '?batch=' + encodeURIComponent(batch) : '';
  window.open('/api/redeem/export.csv' + qs, '_blank');
}
function exportRedeem() {
  const qs = redeemFilterQuery();
  window.open('/api/redeem/export.csv' + (qs ? '?' + qs : ''), '_blank');
}
async function revokeRedeem(value) {
  const actionContext = pageContext('redeem');
  value = uq(value);
  if (!(await deckConfirm('作废这张卡密？作废后无法再用来注册。'))) return;
  try {
    await api(`/api/redeem/${encodeURIComponent(value)}/revoke`, { method: 'POST' });
    toast('已作废');
    renderPage('redeem', true, false, actionContext);
  } catch (e) { toast('作废失败: ' + e.message, 1); }
}

/* ---------- invites and pre-authorisation ----------
   The two ways in that do not involve a card: the operator naming a Telegram
   id, or a member spending an invite slot. Both are shown next to the channel
   switches, because "why can nobody register" is usually one of them being
   off. */
async function loadInviteMembers(context) {
  const members = [];
  let page = 1;
  while (context.isCurrent()) {
    const listing = await api(`/api/members?page=${page}&page_size=200`);
    const rows = listing.members || [];
    members.push(...rows);
    if (!rows.length || members.length >= listing.total) break;
    page++;
  }
  return {members};
}
PAGES.invites = async (context = pageContext('invites')) => {
  $('#view').innerHTML = pageLoading();
  const [grants, listing, tg] = await Promise.all([
    api('/api/registration/grants'),
    loadInviteMembers(context),
    api('/api/settings/telegram'),
  ]);
  if (!context.isCurrent()) return;
  const members = (listing.members || []).slice()
    .sort((a, b) => String(a.username).localeCompare(String(b.username)));
  state.inviteMembers = members;
  const pending = grants.filter((g) => !g.used_at).length;
  const withQuota = members.filter((m) => (m.invite_quota || 0) > 0);
  $('#view').innerHTML = `
    <div class="stat-grid">
      ${stat('🎫', pending, '待使用授权', `共 ${grants.length} 条`)}
      ${stat('☺', withQuota.length, '持有邀请名额的成员',
    `合计 ${withQuota.reduce((n, m) => n + (m.invite_quota || 0), 0)} 个名额`)}
      ${stat('🌳', members.filter((m) => m.register_via === 'invite').length,
    '通过邀请加入', '占比按当前列表')}
    </div>
    ${card('注册通道', '三个通道各自独立；全部关闭等于停止注册',
    `<div class="card-body">
        <div class="form-row"><label for="ch-admin">管理员预授权</label>
          <input id="ch-admin" type="checkbox" ${tg.allow_admin_grant ? 'checked' : ''}>
          <span class="muted">名单里的 Telegram 账号无需任何凭证</span></div>
        <div class="form-row"><label for="ch-invite">邀请码</label>
          <input id="ch-invite" type="checkbox" ${tg.allow_invite ? 'checked' : ''}>
          <span class="muted">老用户用自己的名额生成</span></div>
        <div class="form-row"><label for="ch-redeem">卡密</label>
          <input id="ch-redeem" type="checkbox" ${tg.allow_redeem ? 'checked' : ''}>
          <span class="muted">管理员生成，见「卡密管理」</span></div>
        ${tg.enabled ? '' : '<div class="help">机器人当前未启用，通道开关不会生效。</div>'}
        <div class="toolbar"><button class="btn primary" id="ch-save">保存</button></div>
      </div>`)}
    ${card('管理员预授权', '直接放行某个 Telegram 账号，不需要邀请码或卡密',
    `<div class="card-body">
        <div class="form-row"><label for="gr-id">Telegram ID</label>
          <input id="gr-id" placeholder="纯数字，例如 6425070392" style="max-width:260px">
          <button class="btn primary" id="gr-add">授权</button></div>
        <div class="help">让对方发 /start 给机器人，机器人会回显他的数字 ID。</div>
        ${grants.length ? `<table><thead><tr><th>Telegram ID</th><th>状态</th><th>授权人</th><th>时间</th><th></th></tr></thead><tbody>
          ${grants.map((g) => `<tr>
            <td>${esc(g.tg_user_id)}</td>
            <td>${g.used_at ? '<span class="tag idle">已使用</span>' : '<span class="tag ok">待使用</span>'}</td>
            <td>${esc(g.granted_by || '—')}</td>
            <td>${esc(fmtAgeTs(g.created_at))}</td>
            <td class="icon-actions"><button class="btn sm danger" aria-label="撤销" title="撤销"
              onclick="revokeGrant('${q(g.tg_user_id)}')">🗑</button></td>
          </tr>`).join('')}</tbody></table>` : '<div class="empty">还没有预授权的账号</div>'}
      </div>`)}
    ${card('邀请名额', '发放后成员可在机器人里自助生成邀请码',
    `<div class="card-body">
        <div class="form-row"><label for="iq-member">成员</label>
          <select id="iq-member" style="max-width:260px">${members.map((m) => `<option value="${esc(m.emby_user_id)}">${esc(m.username)}（${m.invite_quota || 0}）</option>`).join('')}</select>
          <input id="iq-delta" aria-label="邀请名额调整数量" type="number" value="1" style="width:90px">
          <button class="btn primary" id="iq-give">发放</button></div>
        <div class="help">填负数即可收回名额；名额不会低于 0。</div>
        ${withQuota.length ? `<table><thead><tr><th>成员</th><th>剩余名额</th><th>已邀请</th><th></th></tr></thead><tbody>
          ${withQuota.map((m) => `<tr>
            <td>${esc(m.username)}</td><td>${esc(m.invite_quota || 0)}</td>
            <td>${esc(m.invitee_count || 0)}</td>
            <td class="icon-actions"><button class="btn sm" aria-label="查看邀请码" title="查看邀请码"
              onclick="showMemberInvites('${q(m.emby_user_id)}','${q(m.username)}')">👁</button></td>
          </tr>`).join('')}</tbody></table>` : '<div class="empty">还没有成员持有邀请名额</div>'}
        <div id="iq-detail"></div>
      </div>`)}`;
  bindAsyncButton('ch-save', saveChannels);
  bindAsyncButton('gr-add', addGrant);
  bindAsyncButton('iq-give', giveQuota);
};
async function saveChannels() {
  const actionContext = pageContext('invites');
  try {
    await api('/api/settings/telegram', {
      method: 'POST',
      body: JSON.stringify({
        bot_token: SECRET_KEEP,
        allow_admin_grant: (($('#ch-admin') || {}).checked) || false,
        allow_invite: (($('#ch-invite') || {}).checked) || false,
        allow_redeem: (($('#ch-redeem') || {}).checked) || false,
      }) });
    toast('已保存');
    renderPage('invites', true, false, actionContext);
  } catch (e) { toast('保存失败: ' + e.message, 1); }
}
async function addGrant() {
  const actionContext = pageContext('invites');
  const value = (($('#gr-id') || {}).value || '').trim();
  if (!value) { toast('请填写 Telegram ID', 1); return; }
  try {
    await api('/api/registration/grants', {
      method: 'POST', body: JSON.stringify({ tg_user_id: value }) });
    toast('已授权');
    renderPage('invites', true, false, actionContext);
  } catch (e) { toast('授权失败: ' + e.message, 1); }
}
async function revokeGrant(value) {
  const actionContext = pageContext('invites');
  value = uq(value);
  if (!(await deckConfirm('撤销这条预授权？对方将无法直接注册。'))) return;
  try {
    await api(`/api/registration/grants/${encodeURIComponent(value)}`,
      { method: 'DELETE' });
    toast('已撤销');
    renderPage('invites', true, false, actionContext);
  } catch (e) { toast('撤销失败: ' + e.message, 1); }
}
async function giveQuota() {
  const actionContext = pageContext('invites');
  const id = ($('#iq-member') || {}).value || '';
  const delta = Number(($('#iq-delta') || {}).value || 0);
  if (!id || !delta) { toast('请选择成员并填写数量', 1); return; }
  try {
    const r = await api(`/api/members/${encodeURIComponent(id)}/invite-quota`, {
      method: 'POST', body: JSON.stringify({ delta }) });
    toast(`名额已更新为 ${r.quota}`);
    renderPage('invites', true, false, actionContext);
  } catch (e) { toast('发放失败: ' + e.message, 1); }
}
async function showMemberInvites(id, username) {
  id = uq(id); username = uq(username);
  const box = $('#iq-detail');
  if (!box) return;
  box.innerHTML = '<div class="page-loading">加载中…</div>';
  try {
    const d = await api(`/api/members/${encodeURIComponent(id)}/invites`);
    const codes = d.invites || [];
    const kids = d.invitees || [];
    box.innerHTML = `
      <div class="help" style="margin-top:16px"><b>${esc(username)}</b> · 剩余名额 ${esc(d.quota)}</div>
      ${codes.length ? `<table><thead><tr><th>邀请码</th><th>剩余次数</th><th>有效期</th><th>状态</th></tr></thead><tbody>
        ${codes.map((c) => `<tr>
          <td><button type="button" class="code-cell linkish" onclick="copyRedeem('${q(c.code)}', this)">${esc(c.masked)}</button></td>
          <td>${esc(c.uses_left)}</td>
          <td>${c.expires_at ? esc(fmtExpiry(c.expires_at)) : '永久'}</td>
          <td>${c.revoked ? '<span class="tag bad">已作废</span>'
    : (c.usable ? '<span class="tag ok">可用</span>' : '<span class="tag idle">已用完</span>')}</td>
        </tr>`).join('')}</tbody></table>` : '<div class="empty">还没有生成过邀请码</div>'}
      ${kids.length ? `<div class="help" style="margin-top:12px">已邀请 ${kids.length} 人：${
  kids.map((k) => esc(k.username)).join('、')}</div>` : ''}`;
  } catch (e) { box.innerHTML = `<div class="help">加载失败：${esc(e.message)}</div>`; }
}

PAGES.tggroup = async (context = pageContext('tggroup')) => {
  $('#view').innerHTML = `${card('群组与频道成员检测','所有启用关联项都必须满足；仅核查Deck已绑定TG会员，不枚举群/频道全员', `<div class="card-body">
    <div class="toolbar"><button class="btn" onclick="go('tgbot?section=membership')">关联设置</button><button class="btn primary" id="gm-scan">开始检测并按开关处理</button><span id="gm-scan-state" role="status"></span></div>
    <p class="help" id="gm-scan-policy"></p><div id="gm-scan-progress"></div><div id="gm-scan-results"></div><div id="gm-event-result"></div></div>`)}
    <details><summary>旧注册要求群核查（只报告，保持原行为）</summary>${card('群组核查','只检查原require_group，不执行本次关联删除规则',`<div class="card-body"><button class="btn" id="ga-run">开始旧核查</button><span id="ga-status"></span><div id="ga-result"></div></div>`)}</details>`;
  bindAsyncButton('ga-run', runGroupAudit);
  let data = null;
  const labels = {present:'符合要求',absent:'不符合要求',unknown:'无法核实',exempt:'管理员豁免',unbound:'未绑定TG'};
  const actions = {detected:'仅检测，未删除',kept:'保留账号',rechecking:'执行前复核',deleted:'已删除本人',failed_retained:'删除失败，本地保留',cancelled:'条件变化，已取消'};
  const render = value => {
    data=value;
    const scan=value.scan||{}, rows=scan.rows||[], on=value.rules?.delete_enabled;
    $('#gm-scan-policy').textContent=on?'删除开关已开启：包括未观测离群的存量会员，明确不符合时立即再次核实并删除本人。仅Deck/Emby管理员豁免，白名单适用；历史保留、不连带。':'删除开关关闭：手动/定时只检测，不删除账号。查询未知始终保留账号。';
    $('#gm-scan').disabled=!!scan.running;
    $('#gm-scan-state').textContent=scan.running?(scan.current?(scan.current.action==='rechecking'?'执行前复核：':'正在检测：')+scan.current.username:'检测进行中'):scan.id?(scan.cancelled||scan.interrupted?'已中止':'检测已结束'):'尚未检测';
    const counts=Object.keys(labels).map(key=>`${labels[key]} ${rows.filter(r=>r.state===key).length}`).join(' · ');
    $('#gm-scan-progress').innerHTML=`<p>${esc(counts)}</p><p>进度 ${Number(scan.processed||0)} / ${Number(scan.total||0)} ${scan.started_at?'· 开始于 '+esc(new Date(scan.started_at*1000).toLocaleString()):''}</p>${scan.running?`<progress max="${Math.max(1,Number(scan.total||0))}" value="${Number(scan.processed||0)}" style="width:100%"></progress>`:''}${scan.error?`<p class="danger-text">检测异常：${esc(scan.error)}；未完成账号保持原状</p>`:''}`;
    $('#gm-scan-results').innerHTML=rows.length?`<div class="table-wrap"><table><thead><tr><th>账号 / TG</th><th>用户组</th><th>逐项核查</th><th>检测结果</th><th>删除动作</th></tr></thead><tbody>${rows.map(row=>`<tr><td><b>${esc(row.username)}</b><small class="muted"> ${esc(row.tg_user_id||'未绑定')}</small></td><td>${esc(row.group_id==='whitelist'?'💠 白名单':row.group_id)}</td><td>${(row.targets||[]).map(t=>`${esc(t.title||t.chat_id)}：${esc(labels[t.state]||'无法核实')}`).join('<br>')}</td><td>${esc(labels[row.state]||'无法核实')}</td><td>${esc(actions[row.action]||row.action||'未删除')}</td></tr>`).join('')}</tbody></table></div>`:'<div class="empty">尚无成员检测结果</div>';
    const event=value.last_event;
    $('#gm-event-result').textContent=event?'最近事件处理：'+event.username+' · '+(labels[event.state]||'无法核实')+' · '+(actions[event.action]||event.action):'尚无离群事件处理记录；旧通知不会迁移为删除任务。';
  };
  const refresh=async()=>{
    try{const value=await api('/api/telegram/membership');if(!context.isCurrent())return;render(value);if(value.scan?.running)setTimeout(refresh,1000);}
    catch(err){if(context.isCurrent())$('#gm-scan-state').textContent='读取失败：'+err.message;}
  };
  $('#gm-scan').onclick=async()=>{
    if(data?.rules?.delete_enabled&&!(await deckConfirm('删除开关已开启：本次检测会重新核实并删除不合规存量会员本人（含白名单），历史保留，不连带。继续？')))return;
    $('#gm-scan').disabled=true;
    try{await api('/api/telegram/membership/scan',{method:'POST'});if(context.isCurrent())await refresh();}
    catch(err){if(context.isCurrent()){$('#gm-scan-state').textContent=err.message;$('#gm-scan').disabled=false;}}
  };
  await refresh();
};
async function runGroupAudit() {
  const st = $('#ga-status');
  const box = $('#ga-result');
  st.textContent = '核查中…（成员较多时需要一会儿）';
  box.innerHTML = '';
  try {
    const r = await api('/api/telegram/group-audit', { method: 'POST' });
    if (r.unavailable) {
      st.innerHTML = '<span class="tag idle">未配置群组</span>';
      box.innerHTML = '<div class="empty">先在「机器人」页填写要求群组</div>';
      return;
    }
    st.innerHTML = `<span class="tag ok">已核查 ${r.checked} 人</span>`;
    box.innerHTML = (r.left || []).length
      ? `<table><thead><tr><th>用户</th><th>Telegram</th><th>状态</th></tr></thead><tbody>
          ${r.left.map((m) => `<tr><td>${esc(m.username || '-')}</td>
            <td>${esc(m.tg_user_id)}</td>
            <td><span class="tag warn">${esc(m.status || '已离开')}</span></td></tr>`).join('')}
        </tbody></table>`
      : '<div class="empty">所有已关联成员都还在群里</div>';
  } catch (e) {
    st.innerHTML = `<span class="tag bad">核查失败</span> ${esc(e.message)}`;
  }
}

/* ---------- 自动化 ----------
   Every scheduled job is a plugin, and this page is generated from what the
   backend declares rather than hand-written per feature: a card, its form, its
   schedule line and its last result all come from the same payload. Adding a
   job to the panel means adding a file on the server, not editing this file. */

const automation = { category: 'task', open: {}, busy: {} };

const PLUGIN_CATEGORIES = [
  { id: 'task', label: '任务' },
  { id: 'points', label: '积分' },
  { id: 'request', label: '求片' },
];

PAGES.automation = async (context = pageContext('automation')) => {
  $('#view').innerHTML = pageLoading();
  const cards = await api(`/api/plugins?category=${encodeURIComponent(automation.category)}`)
    .catch(() => null);
  if (!context.isCurrent()) return;
  if (!cards) { $('#view').innerHTML = pageError('无法读取任务列表'); return; }

  const tabs = PLUGIN_CATEGORIES.map((c) =>
    `<button class="btn ${c.id === automation.category ? 'primary' : ''}"
       onclick="switchPluginCategory('${c.id}')">${esc(c.label)}</button>`).join('');

  const emptyLabel = ({ points: '还没有已注册的积分功能',
    request: '还没有已注册的求片任务' })[automation.category]
    || '还没有已注册的任务';
  const body = cards.length
    ? `<div class="plugin-grid">${cards.map(pluginCard).join('')}</div>`
    : `<div class="card"><div class="empty">${emptyLabel}</div></div>`;

  $('#view').innerHTML = `
    <div class="help">${{
    points: `积分功能和定时任务共用同一套开关与配置。<b>签到和转账由成员在机器人里触发</b>，
       这里的「立即运行」只统计不发放；<b>关掉开关，机器人里对应的按钮就会消失</b>。`,
    request: `求片相关的定时任务。<b>每条求片在提交时就会推给上片员</b>，
       这里的摘要只是每天提醒一次还有多少没人接，避免没人接的求片一直没动静。`,
  }[automation.category]
    || `定时任务在这里统一开关和配置。<b>「立即运行」不看开关</b>：先试一次再决定要不要常开。
       任何一个任务出错都只影响它自己的卡片，不会影响其他任务。`}
    </div>
    <div class="toolbar" style="margin-bottom:14px">${tabs}</div>
    ${body}`;

  cards.forEach(bindPluginCard);
};

function switchPluginCategory(id) {
  automation.category = id;
  renderPage('automation');
}

function pluginScheduleText(c) {
  if (c.hour !== null && c.hour !== undefined) {
    const hour = (c.config && c.config.hour !== undefined) ? c.config.hour : c.hour;
    return `每天 ${esc(String(hour))}:00`;
  }
  if (c.interval > 0) return `每 ${fmtAge(c.interval)}`;
  return '仅手动';
}

function pluginField(pid, f, value) {
  const id = `pl-${pid}-${f.key}`;
  const v = value === undefined ? f.default : value;
  let input;
  if (f.kind === 'bool') {
    input = `<input id="${id}" type="checkbox" ${v ? 'checked' : ''}>`;
  } else if (f.kind === 'int') {
    const min = f.min === undefined ? '' : ` min="${esc(f.min)}"`;
    const max = f.max === undefined ? '' : ` max="${esc(f.max)}"`;
    input = `<input id="${id}" type="number"${min}${max} value="${esc(v)}" style="width:110px">`;
  } else if (f.kind === 'select') {
    input = `<select id="${id}">${(f.options || []).map((o) =>
      `<option value="${esc(o.value)}" ${o.value === v ? 'selected' : ''}>${esc(o.label)}</option>`
    ).join('')}</select>`;
  } else if (f.kind === 'text') {
    input = `<textarea id="${id}" rows="3" style="flex:1;min-width:240px">${esc(v)}</textarea>`;
  } else {
    input = `<input id="${id}" value="${esc(v)}" style="flex:1;min-width:200px">`;
  }
  return `<div class="form-row">
    <label for="${esc(id)}">${esc(f.label)}</label>${input}
    ${f.help ? `<span class="muted">${esc(f.help)}</span>` : ''}
  </div>`;
}

/* A result the operator can check: when it ran, whether it worked, what it did
   and how long it took. A job reporting nothing is indistinguishable from a job
   that never ran, which is the failure mode that goes unnoticed for weeks. */
function pluginLastRun(c) {
  const last = c.last_run;
  if (!last) return '<div class="muted">尚未运行过</div>';
  const summary = last.summary || {};
  const kv = Object.keys(summary).map((k) =>
    `<span class="kv"><b>${esc(k)}</b>${esc(String(summary[k]))}</span>`).join('');
  return `<div class="plugin-last">
    <div>
      ${last.ok ? '<span class="tag ok">成功</span>' : '<span class="tag bad">失败</span>'}
      <span class="muted">${esc(fmtAgeTs(last.started_at))} ·
        ${esc(last.trigger === 'manual' ? '手动' : '定时')} ·
        ${esc(Math.round(Number(last.duration_ms || 0)))} ms</span>
    </div>
    ${kv ? `<div class="kv-row">${kv}</div>` : ''}
  </div>`;
}

function pluginCard(c) {
  const busy = automation.busy[c.id];
  const open = automation.open[c.id];
  return `<div class="card plugin-card" data-plugin="${esc(c.id)}">
    <div class="card-head">
      <div style="display:flex;gap:10px;align-items:flex-start">
        <div class="ic-box">${c.icon || '⚙'}</div>
        <div>
          <h3>${esc(c.name)}</h3>
          <div class="sub">${esc(pluginScheduleText(c))}</div>
        </div>
      </div>
      <label class="plugin-switch">
        <input id="pl-${esc(c.id)}-enabled" type="checkbox" ${c.enabled ? 'checked' : ''}>
        <span class="muted">${c.enabled ? '已启用' : '已停用'}</span>
      </label>
    </div>
    <div class="card-body">
      <div class="muted" style="line-height:1.6;margin-bottom:12px">${esc(c.description)}</div>
      ${(c.fields || []).map((f) => pluginField(c.id, f, (c.config || {})[f.key])).join('')}
      <div class="toolbar" style="margin-top:12px">
        <button class="btn primary" data-act="save" ${busy ? 'disabled' : ''}>保存</button>
        <button class="btn" data-act="run" ${busy ? 'disabled' : ''}>
          ${busy ? '运行中…' : '立即运行'}</button>
        <button class="btn sm" data-act="history">${open ? '收起历史' : '历史'}</button>
      </div>
      <div class="plugin-result" style="margin-top:12px">${pluginLastRun(c)}</div>
      <div class="plugin-history" data-history="${esc(c.id)}">
        ${open ? '<div class="muted">读取中…</div>' : ''}
      </div>
    </div>
  </div>`;
}

function pluginCardEl(pid) {
  return document.querySelector(`.plugin-card[data-plugin="${pid}"]`);
}

function bindPluginCard(c) {
  const el = pluginCardEl(c.id);
  if (!el) return;
  el.querySelector('[data-act="save"]').onclick = () => savePlugin(c);
  el.querySelector('[data-act="run"]').onclick = () => runPlugin(c);
  el.querySelector('[data-act="history"]').onclick = () => togglePluginHistory(c);
  if (automation.open[c.id]) loadPluginHistory(c.id);
}

function pluginPayload(c) {
  const config = {};
  (c.fields || []).forEach((f) => {
    const el = $(`#pl-${c.id}-${f.key}`);
    if (!el) return;
    config[f.key] = f.kind === 'bool' ? el.checked : el.value;
  });
  const sw = $(`#pl-${c.id}-enabled`);
  return { enabled: sw ? sw.checked : c.enabled, config };
}

async function savePlugin(c, run = false) {
  const el = pluginCardEl(c.id);
  if (!el || automation.busy[c.id]) return;
  const invalid = [...el.querySelectorAll('input,select,textarea')].find(input => !input.checkValidity());
  if (invalid) { invalid.reportValidity(); return; }
  // Capture before any DOM update, and never reload unrelated plugin editors.
  const payload = pluginPayload(c);
  const buttons = [...el.querySelectorAll('[data-act="save"],[data-act="run"]')];
  automation.busy[c.id] = true;
  buttons.forEach(button => { button.disabled = true; });
  configFeedback(el, run ? '正在保存并运行…' : '正在保存…');
  try {
    const saved = await api(`/api/plugins/${encodeURIComponent(c.id)}`, {
      method: 'POST', body: JSON.stringify(payload) });
    Object.assign(c, saved);
    if (run) {
      const r = await api(`/api/plugins/${encodeURIComponent(c.id)}/run`, { method: 'POST' });
      // The run response includes the recorded result, even on task failure.
      if (r.card && el.isConnected) el.querySelector('.plugin-result').innerHTML = pluginLastRun(r.card);
      if (!r.ok) throw new Error(r.error || '任务运行失败，见运行历史');
    }
    if (!el.isConnected) return;
    el.querySelector('.plugin-switch span').textContent = c.enabled ? '已启用' : '已停用';
    el.querySelector('.card-head .sub').textContent = pluginScheduleText(c);
    configFeedback(el, run ? '运行完成' : '已保存；其他草稿仍保留。');
    if (automation.open[c.id] && el.isConnected) loadPluginHistory(c.id);
  } catch (e) {
    if (el.isConnected) configFeedback(el, (run ? '运行失败：' : '保存失败：') + e.message + '。输入已保留，可重试。', true);
    toast('操作失败: ' + e.message, 1);
  } finally {
    automation.busy[c.id] = false;
    buttons.forEach(button => { button.disabled = false; });
  }
}
function runPlugin(c) { return savePlugin(c, true); }

function togglePluginHistory(c) {
  const el = pluginCardEl(c.id);
  if (!el) return;
  automation.open[c.id] = !automation.open[c.id];
  el.querySelector('[data-act="history"]').textContent = automation.open[c.id] ? '收起历史' : '历史';
  const box = el.querySelector('.plugin-history');
  box.hidden = !automation.open[c.id];
  if (automation.open[c.id]) loadPluginHistory(c.id);
}

async function loadPluginHistory(pid) {
  const box = document.querySelector(`[data-history="${pid}"]`);
  if (!box) return;
  try {
    const rows = await api(`/api/plugins/${encodeURIComponent(pid)}/history?limit=10`);
    box.innerHTML = rows.length
      ? `<table><thead><tr><th>时间</th><th>结果</th><th>触发</th><th>耗时</th><th>摘要</th></tr></thead>
         <tbody>${rows.map((r) => `<tr>
           <td>${esc(fmtAgeTs(r.started_at))}</td>
           <td>${r.ok ? '<span class="tag ok">成功</span>' : '<span class="tag bad">失败</span>'}</td>
           <td>${esc(r.trigger === 'manual' ? '手动' : '定时')}</td>
           <td>${esc(Math.round(Number(r.duration_ms || 0)))} ms</td>
           <td class="muted">${esc(JSON.stringify(r.summary || {}))}</td>
         </tr>`).join('')}</tbody></table>`
      : '<div class="empty">还没有运行记录</div>';
  } catch (e) {
    box.innerHTML = `<div class="muted">历史读取失败：${esc(e.message)}</div>`;
  }
}

/* ---------- 安全 ----------
   Two pages that share one principle: the panel sits on the media path, so it
   reports far more readily than it acts. Access rules are the one place it does
   refuse, which is why the block log is shown right next to them -- a refusal
   that leaves no trace is indistinguishable from a broken node. */

PAGES.access = async (context = pageContext('access')) => {
  $('#view').innerHTML = pageLoading();
  const [rules, blocks] = await Promise.all([
    api('/api/access/rules').catch(() => null),
    api('/api/access/blocks?limit=100'),
  ]);
  if (!context.isCurrent()) return;
  if (!rules) { $('#view').innerHTML = pageError('无法读取访问规则'); return; }

  const dayAgo = (Date.now() / 1000) - 86400;
  const todayBlocks = blocks.filter((b) => Number(b.blocked_at || 0) >= dayAgo).length;
  const kindLabel = (k) => (k === 'network' ? '网段' : '客户端');

  $('#view').innerHTML = `
    <div class="help">
      规则只在播放请求上生效。<b>出错一律放行</b>：面板挡在播放链路上，
      写错一条正则不能让所有人看不了片。被拒的请求都会记录在下面。
    </div>
    <div class="stat-grid">
      ${stat('🛡', rules.length, '规则数', `${rules.filter((r) => r.enabled).length} 条已启用`)}
      ${stat('⛔', todayBlocks, '今日拦截', `累计 ${blocks.length} 条记录`)}
    </div>
    ${card('新增规则', '客户端按 User-Agent 正则匹配；网段填单个地址或 CIDR',
      `<div class="card-body">
        <div class="form-row"><label for="ac-kind">类型</label>
          <select id="ac-kind">
            <option value="client">客户端（User-Agent）</option>
            <option value="network">网段（IP / CIDR）</option>
          </select></div>
        <div class="form-row"><label for="ac-pattern">内容</label>
          <input id="ac-pattern" placeholder="例如 curl|wget 或 203.0.113.0/24"></div>
        <div class="form-row"><label for="ac-action">动作</label>
          <select id="ac-action">
            <option value="deny">拒绝</option>
            <option value="allow">放行</option>
          </select>
          <span class="muted">放行规则优先于拒绝，用来给例外开口子</span></div>
        <div class="form-row"><label for="ac-note">备注</label>
          <input id="ac-note" placeholder="写清楚为什么加这条，几个月后你会需要"></div>
        <div class="toolbar"><button class="btn primary" id="ac-add">添加规则</button></div>
      </div>`)}
    ${tableCard('规则列表', `${rules.length} 条`,
      ['类型', '内容', '动作', '备注', '启用', ''],
      rules.map((r) => `<tr>
        <td><span class="tag idle">${esc(kindLabel(r.kind))}</span></td>
        <td><code>${esc(r.pattern)}</code></td>
        <td>${r.action === 'deny'
          ? '<span class="tag bad">拒绝</span>' : '<span class="tag ok">放行</span>'}</td>
        <td class="muted">${esc(r.note || '-')}</td>
        <td><input type="checkbox" ${r.enabled ? 'checked' : ''}
          aria-label="启用规则 ${esc(r.pattern)}" onchange="toggleAccessRule(${r.id}, this.checked, this)"></td>
        <td class="row-actions">
          <button class="btn sm danger" onclick="deleteAccessRule(${r.id})">删除</button>
        </td></tr>`).join(''))}
    ${tableCard('拦截记录', '最近 100 条', ['时间', '用户', '客户端', '地址', '命中规则'],
      blocks.map((b) => `<tr>
        <td>${esc(fmtAgeTs(b.blocked_at))}</td>
        <td>${esc(b.username || '-')}</td>
        <td class="muted" title="${esc(b.user_agent || '')}">${esc((b.user_agent || '-').slice(0, 60))}</td>
        <td>${esc(b.remote_ip || '-')}</td>
        <td class="muted">${esc(b.reason || '')}${b.rule_id ? ` (#${esc(b.rule_id)})` : ''}</td>
      </tr>`).join(''))}`;
  bindAsyncButton('ac-add', addAccessRule);
};

async function addAccessRule() {
  const actionContext = pageContext('access');
  const pattern = $('#ac-pattern').value.trim();
  if (!pattern) { toast('请填写规则内容', 1); return; }
  try {
    await api('/api/access/rules', { method: 'POST', body: JSON.stringify({
      kind: $('#ac-kind').value, pattern, action: $('#ac-action').value,
      note: $('#ac-note').value.trim(), enabled: true }) });
    toast('规则已添加');
    renderPage('access', true, false, actionContext);
  } catch (e) {
    // A bad regex or netmask comes back as a 400 with the reason; showing it
    // verbatim is the difference between fixing it and guessing.
    toast('添加失败: ' + e.message, 1);
  }
}

async function toggleAccessRule(id, enabled, input) {
  const actionContext = pageContext('access');
  if (input?.disabled) return;
  if (input) input.disabled = true;
  try {
    await api(`/api/access/rules/${id}/enabled`, {
      method: 'POST', body: JSON.stringify({ enabled }) });
    toast(enabled ? '规则已启用' : '规则已停用');
    renderPage('access', false, false, actionContext);
  } catch (e) { if (input) input.checked = !enabled; toast('操作失败: ' + e.message, 1); }
  finally { if (input) input.disabled = false; }
}

async function deleteAccessRule(id) {
  const actionContext = pageContext('access');
  if (!(await deckConfirm('删除这条规则？'))) return;
  try {
    await api(`/api/access/rules/${id}`, { method: 'DELETE' });
    toast('规则已删除');
    renderPage('access', true, false, actionContext);
  } catch (e) { toast('删除失败: ' + e.message, 1); }
}

PAGES.sharing = async (context = pageContext('sharing')) => {
  $('#view').innerHTML = pageLoading();
  const data = await api('/api/sharing?limit=50').catch(() => null);
  if (!context.isCurrent()) return;
  if (!data) { $('#view').innerHTML = pageError('无法读取共享检测结果'); return; }
  const st = data.status || {};
  const items = data.items || [];
  $('#view').innerHTML = `
    <div class="help">
      同一账号同时在多个网络播放时会记在这里。<b>只记录不处理</b>：
      一家人有电视和手机，手机从 Wi-Fi 切到流量也会算成两个网络，
      按这个自动封号会误伤付费用户。判断留给人。
    </div>
    <div class="stat-grid">
      ${stat('👥', st.tracked_accounts || 0, '追踪账号', '当前有播放活动的账号')}
      ${stat('⚠', st.multi_network_now || 0, '多地播放', '此刻同时在多个网络')}
      ${stat('☰', items.length, '历史发现', `网络需持续 ${fmtAge(st.min_network_seconds || 0)}才计入`)}
    </div>
    ${tableCard('发现记录', '最近 50 条', ['时间', '用户', '网络数', '网络列表'],
      items.map((it) => `<tr>
        <td>${esc(fmtAgeTs(it.detected_at))}</td>
        <td>${esc(it.username || it.emby_user_id || '-')}</td>
        <td><span class="tag warn">${esc(it.network_count)}</span></td>
        <td class="muted">${(it.networks || []).map((n) => esc(n)).join(' · ')}</td>
      </tr>`).join(''))}`;
};

/* ---------- 兑换商城 ----------
   Items are data, so this page is a plain editor over them: the operator
   prices and retires things without a release. The orders table underneath is
   the other half -- a catalogue with no record of what it handed out cannot
   answer "why does this member have 500GB extra". */
const SHOP_KINDS = [
  { id: 'traffic', label: '流量包', unit: 'GB' },
  { id: 'days', label: '会员天数', unit: '天' },
  { id: 'bandwidth', label: '带宽提速', unit: 'Mbps' },
  { id: 'invite', label: '邀请名额', unit: '个' },
];
function shopUnit(kind) {
  const found = SHOP_KINDS.find((k) => k.id === kind);
  return found ? found.unit : '';
}
PAGES.shop = async (context = pageContext('shop')) => {
  $('#view').innerHTML = pageLoading();
  const [items, orders] = await Promise.all([
    api('/api/shop/items').catch(() => null),
    api('/api/shop/orders?limit=50'),
  ]);
  if (!context.isCurrent()) return;
  if (!items) { $('#view').innerHTML = pageError('无法读取商城商品'); return; }
  // Kept so the edit dialog can prefill from the row already on screen
  // rather than re-fetching one item.
  state.shopItems = items;
  const live = items.filter((i) => i.enabled).length;
  const spent = orders.reduce((sum, o) => sum + Number(o.cost || 0), 0);
  $('#view').innerHTML = `
    <div class="help">
      成员用积分在机器人的「背包 → 兑换商城」里兑换这些商品。
      <b>新建的商品默认要手动开启</b>，开启后成员才看得到；
      带宽提速对<b>不限速</b>的账号无意义，系统会直接拒绝并且不扣分。
    </div>
    <div class="stat-grid">
      ${stat('🎁', items.length, '商品', `${live} 个已上架`)}
      ${stat('📜', orders.length, '兑换记录', '最近 50 条')}
      ${stat('💰', spent, '消耗积分', '这些记录合计')}
    </div>
    ${card('新增商品', '数量的单位随类型变化：流量按 GB，天数按天，提速按 Mbps，名额按个',
    `<div class="card-body">
        <div class="form-row"><label for="sh-kind">类型</label>
          <select id="sh-kind">${SHOP_KINDS.map((k) =>
    `<option value="${esc(k.id)}">${esc(k.label)}</option>`).join('')}</select>
          <span class="muted" id="sh-unit">单位 GB</span></div>
        <div class="form-row"><label for="sh-name">名称</label>
          <input id="sh-name" placeholder="例如「流量包 50GB」"></div>
        <div class="form-row"><label for="sh-desc">说明</label>
          <input id="sh-desc" placeholder="成员在机器人里看到的一句话说明"></div>
        <div class="form-row"><label for="sh-cost">消耗积分</label>
          <input id="sh-cost" type="number" min="1" value="100" style="width:110px"></div>
        <div class="form-row"><label for="sh-amount">数量</label>
          <input id="sh-amount" type="number" min="1" value="50" style="width:110px"></div>
        <div class="form-row"><label for="sh-limit">每人限兑</label>
          <input id="sh-limit" type="number" min="0" value="0" style="width:110px">
          <span class="muted">0 = 不限</span></div>
        <div class="form-row"><label for="sh-sort">排序</label>
          <input id="sh-sort" type="number" value="0" style="width:110px">
          <span class="muted">越小越靠前</span></div>
        <div class="form-row"><label for="sh-enabled">立即上架</label>
          <input id="sh-enabled" type="checkbox"></div>
        <div class="toolbar"><button class="btn primary" id="sh-add">新增商品</button></div>
      </div>`)}
    ${tableCard('商品', `${items.length} 个`,
    ['名称', '类型', '消耗', '数量', '每人限兑', '排序', '上架', ''],
    items.length ? items.map((i) => `<tr>
        <td><div class="u-name">${esc(i.name)}</div>
          ${i.description ? `<div class="u-sub muted">${esc(i.description)}</div>` : ''}</td>
        <td>${esc(i.kind_label || i.kind)}</td>
        <td><b>${esc(i.cost)}</b> 分</td>
        <td>${esc(i.amount)} ${esc(i.unit || shopUnit(i.kind))}</td>
        <td>${i.per_user_limit ? esc(i.per_user_limit) + ' 次' : '<span class="muted">不限</span>'}</td>
        <td>${esc(i.sort)}</td>
        <td><input type="checkbox" ${i.enabled ? 'checked' : ''}
          aria-label="上架 ${esc(i.name)}" onchange="toggleShopItem(${Number(i.id)}, this.checked)"></td>
        <td class="icon-actions">
          <button class="btn sm" aria-label="编辑" title="编辑" onclick="editShopItem(${Number(i.id)})">✎</button>
          <button class="btn sm danger" aria-label="删除" title="删除"
            onclick="deleteShopItem(${Number(i.id)}, '${q(i.name)}')">🗑</button>
        </td></tr>`).join('')
      : '<tr><td colspan="8"><div class="empty">还没有商品</div></td></tr>')}
    ${tableCard('兑换记录', '最近 50 条', ['时间', '用户', '商品', '发放', '消耗'],
    orders.length ? orders.map((o) => `<tr>
        <td>${esc(fmtAgeTs(o.created_at))}</td>
        <td>${esc(o.username || o.emby_user_id || '-')}</td>
        <td>${esc(o.item_name || '-')}</td>
        <td>${esc(o.amount)} ${esc(o.unit || shopUnit(o.kind))}</td>
        <td>-${esc(o.cost)} 分</td></tr>`).join('')
      : '<tr><td colspan="5"><div class="empty">还没有人兑换过</div></td></tr>')}`;
  const kindSel = $('#sh-kind');
  if (kindSel) {
    kindSel.onchange = () => {
      const unit = $('#sh-unit');
      if (unit) unit.textContent = '单位 ' + shopUnit(kindSel.value);
    };
  }
  if ($('#sh-add')) bindAsyncButton('sh-add', addShopItem);
};
function shopFormPayload() {
  return {
    kind: ($('#sh-kind') || {}).value,
    name: (($('#sh-name') || {}).value || '').trim(),
    description: (($('#sh-desc') || {}).value || '').trim(),
    cost: Number(($('#sh-cost') || {}).value || 0),
    amount: Number(($('#sh-amount') || {}).value || 0),
    per_user_limit: Number(($('#sh-limit') || {}).value || 0),
    sort: Number(($('#sh-sort') || {}).value || 0),
    enabled: !!(($('#sh-enabled') || {}).checked),
  };
}
async function addShopItem() {
  const actionContext = pageContext('shop');
  const payload = shopFormPayload();
  if (!payload.name) { toast('请填写商品名称', 1); return; }
  try {
    await api('/api/shop/items', { method: 'POST', body: JSON.stringify(payload) });
    toast('已新增商品'); renderPage('shop', false, false, actionContext);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function toggleShopItem(id, enabled) {
  const actionContext = pageContext('shop');
  try {
    await api(`/api/shop/items/${Number(id)}`, {
      method: 'PUT', body: JSON.stringify({ enabled }) });
    toast(enabled ? '已上架' : '已下架'); renderPage('shop', false, false, actionContext);
  } catch (e) { toast('失败: ' + e.message, 1); renderPage('shop', false, false, actionContext); }
}
function editShopItem(id) {
  const item = ((state.shopItems || []).find((i) => i.id === id));
  const row = item || { id };
  openModal('编辑商品', `
    <div class="form-row"><label for="se-name">名称</label><input id="se-name" value="${esc(row.name || '')}"></div>
    <div class="form-row"><label for="se-desc">说明</label><input id="se-desc" value="${esc(row.description || '')}"></div>
    <div class="form-row"><label for="se-cost">消耗积分</label>
      <input id="se-cost" type="number" min="1" value="${esc(row.cost || 1)}" style="width:110px"></div>
    <div class="form-row"><label for="se-amount">数量</label>
      <input id="se-amount" type="number" min="1" value="${esc(row.amount || 1)}" style="width:110px"></div>
    <div class="form-row"><label for="se-limit">每人限兑</label>
      <input id="se-limit" type="number" min="0" value="${esc(row.per_user_limit || 0)}" style="width:110px">
      <span class="muted">0 = 不限</span></div>
    <div class="form-row"><label for="se-sort">排序</label>
      <input id="se-sort" type="number" value="${esc(row.sort || 0)}" style="width:110px"></div>
    <div class="toolbar"><button class="btn primary" id="se-save">保存</button></div>`);
  const save = $('#se-save');
  if (save) {
    save.onclick = async () => {
      try {
        await api(`/api/shop/items/${Number(id)}`, {
          method: 'PUT',
          body: JSON.stringify({
            name: ($('#se-name') || {}).value,
            description: ($('#se-desc') || {}).value,
            cost: Number(($('#se-cost') || {}).value || 0),
            amount: Number(($('#se-amount') || {}).value || 0),
            per_user_limit: Number(($('#se-limit') || {}).value || 0),
            sort: Number(($('#se-sort') || {}).value || 0),
          }),
        });
        closeModal(); toast('已保存'); renderPage('shop');
      } catch (e) { toast('失败: ' + e.message, 1); }
    };
  }
}
async function deleteShopItem(id, name) {
  const actionContext = pageContext('shop');
  if (!(await deckConfirm(`确定删除商品「${uq(name)}」？已产生的兑换记录会保留。`))) return;
  try {
    await api(`/api/shop/items/${Number(id)}`, { method: 'DELETE' });
    toast('已删除'); renderPage('shop', false, false, actionContext);
  } catch (e) { toast('失败: ' + e.message, 1); }
}

/* ---------- 求片 ----------
   The operator's view of the same queue the uploaders see in Telegram. It
   exists because the bot fan-out only reaches uploaders who linked a chat,
   and somebody has to be able to see and close a request when nobody did. */
const REQUEST_STATUS_TABS = [
  { id: 'active', label: '未处理' },
  { id: 'open', label: '待接单' },
  { id: 'claimed', label: '处理中' },
  { id: 'done', label: '已处理' },
  { id: 'rejected', label: '已拒绝' },
  { id: '', label: '全部' },
];
const requestsView = { status: 'active' };

function requestStatusTag(row) {
  const cls = ({ open: 'warn', claimed: 'idle', done: 'ok',
    rejected: 'bad' })[row.status] || 'idle';
  return `<span class="tag ${cls}">${esc(row.status_label || row.status)}</span>`;
}
function requestPoster(row) {
  if (!row.poster_path) return '<span class="muted">—</span>';
  const src = String(row.poster_path).startsWith('http')
    ? row.poster_path : `https://image.tmdb.org/t/p/w92${row.poster_path}`;
  return `<img src="${esc(src)}" alt="" loading="lazy"
    style="width:34px;height:51px;object-fit:cover;border-radius:3px">`;
}
function requestActions(row) {
  if (row.status === 'open') {
    return `<button class="btn sm" onclick="claimRequest(${Number(row.id)})">接单</button>`;
  }
  if (row.status === 'claimed') {
    return `<button class="btn sm" onclick="resolveRequest(${Number(row.id)},1)">已处理</button>
      <button class="btn sm danger" onclick="resolveRequest(${Number(row.id)},0)">拒绝</button>`;
  }
  return '<span class="muted">—</span>';
}
PAGES.requests = async (context = pageContext('requests')) => {
  $('#view').innerHTML = pageLoading();
  const query = requestsView.status ? `?status=${encodeURIComponent(requestsView.status)}` : '';
  const [rows, stats] = await Promise.all([
    api(`/api/requests${query}`).catch(() => null),
    api('/api/requests/stats'),
  ]);
  if (!context.isCurrent()) return;
  if (!rows) { $('#view').innerHTML = pageError('无法读取求片列表'); return; }

  const tabs = REQUEST_STATUS_TABS.map((t) =>
    `<button class="btn ${t.id === requestsView.status ? 'primary' : ''}"
       onclick="switchRequestStatus('${t.id}')">${esc(t.label)}</button>`).join('');

  $('#view').innerHTML = `
    <div class="help">
      成员在机器人里发 TMDB 链接求片，<b>每条求片会单独发给每个已关联 Telegram 的上片员</b>，
      谁先点「接单」谁负责，其他人的按钮会自动收回。
      这里可以代为接单或关闭；关闭后求片人会收到通知。
      <b>没有配置 TMDB Key 也能用</b>，只是显示编号而不是片名。
    </div>
    <div class="stat-grid">
      ${stat('🕓', stats.open || 0, '待接单', '还没有人认领')}
      ${stat('🔧', stats.claimed || 0, '处理中', '已有上片员接单')}
      ${stat('✅', stats.done || 0, '已处理', '累计')}
      ${stat('📅', stats.month_total || 0, '本月求片', stats.period || '')}
    </div>
    <div class="toolbar" style="margin-bottom:14px">${tabs}</div>
    ${tableCard('求片列表', `${rows.length} 条`,
    ['编号', '海报', '片名', '类型', '求片人', '状态', '接单人', '时间', ''],
    rows.map((r) => `<tr>
        <td>#${esc(r.id)}</td>
        <td>${requestPoster(r)}</td>
        <td><div class="u-name">${esc(r.display_title)}</div>
          <div class="u-sub muted">TMDB ${esc(r.tmdb_id)}${r.note ? ' · ' + esc(r.note) : ''}</div>
          ${r.result_note ? `<div class="u-sub muted">结果：${esc(r.result_note)}</div>` : ''}</td>
        <td>${esc(r.media_label || '-')}</td>
        <td>${esc(r.username || r.emby_user_id || '-')}</td>
        <td>${requestStatusTag(r)}</td>
        <td>${r.claimed_by_name ? esc(r.claimed_by_name) : '<span class="muted">—</span>'}</td>
        <td class="muted">${esc(fmtAgeTs(r.created_at))}</td>
        <td class="row-actions">${requestActions(r)}</td>
      </tr>`).join(''))}`;
};
function switchRequestStatus(id) {
  requestsView.status = id;
  renderPage('requests');
}
async function claimRequest(id) {
  const actionContext = pageContext('requests');
  try {
    const r = await api(`/api/requests/${Number(id)}/claim`, {
      method: 'POST', body: JSON.stringify({}) });
    /* A lost race is a normal outcome, not an error: somebody in Telegram
       may have tapped 接单 while this page was open. */
    toast(r && r.ok === false
      ? `已被 ${r.claimed_by_name || '其他上片员'} 接单`
      : '已接单');
    renderPage('requests', false, false, actionContext);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
async function resolveRequest(id, done) {
  const actionContext = pageContext('requests');
  let note = '';
  if (!done) {
    note = await deckPrompt('无法处理的原因？会原样发给求片人。', '暂时找不到片源');
    if (note === null) return;
  } else if (!(await deckConfirm('标记为已处理？求片人会收到通知。'))) {
    return;
  }
  try {
    await api(`/api/requests/${Number(id)}/resolve`, {
      method: 'POST', body: JSON.stringify({ done: !!done, note }) });
    toast(done ? '已标记处理完成' : '已拒绝并通知求片人');
    renderPage('requests', false, false, actionContext);
  } catch (e) { toast('失败: ' + e.message, 1); }
}
