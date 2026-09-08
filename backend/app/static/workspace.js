/* Six workspaces, progressively disclosed configuration, and scoped saves.
   No framework, no configuration defaults, and no change to live DOM reconciliation. */
const configBaselines = new WeakMap();
let configRoot = null;
function fmtWatchSeconds(value) {
  if (value == null) return '暂无统计';
  const n = Math.max(0, Math.floor(Number(value))), h = Math.floor(n / 3600), m = Math.floor(n % 3600 / 60);
  return h ? `${h}小时${m}分` : m ? `${m}分` : `${n}秒`;
}
function watchWindowLabel(watch, window) {
  if (!watch) return '暂无统计';
  const n = watch['seconds_' + window];
  if (watch['incomplete_' + window]) return n ? `已确认 ${esc(fmtWatchSeconds(n))}<small class="muted"> · 部分跨界历史无法拆分</small>` : '有跨界历史，时长无法完整还原';
  return esc(fmtWatchSeconds(n));
}
function actualBytes(n) { return n == null ? '暂无实测记录' : fmtBytes(n); }
function controlValue(el) { return el.type === 'checkbox' ? el.checked : el.value; }
function configDirty() {
  return configRoot?.isConnected && [...configRoot.querySelectorAll('input,select,textarea')]
    .some(el => configBaselines.has(el) && configBaselines.get(el) !== controlValue(el));
}
function configCanLeave() { return !configDirty() || confirm('当前有未保存的配置。离开会丢弃这些修改，仍要离开吗？'); }
window.addEventListener('beforeunload', e => { if (configDirty()) { e.preventDefault(); e.returnValue = ''; } });
// Auxiliary preview/export controls do not represent persisted settings.
function configFeedback(section, message, bad = false) {
  let el = section.querySelector('.save-feedback');
  if (!el) { el = document.createElement('p'); el.className = 'save-feedback'; el.setAttribute('role', 'status'); section.append(el); }
  el.classList.toggle('danger-text', bad); el.textContent = message;
}
function configureSave(buttonId, path, method, payload, after) {
  const button = document.getElementById(buttonId);
  if (!button) return;
  const section = button.closest('.card');
  button.onclick = async () => {
    if (section.dataset.saving) return;
    const controls = [...section.querySelectorAll('input,select,textarea')].filter(el => configBaselines.has(el));
    const invalid = controls.find(el => !el.checkValidity());
    if (invalid) { invalid.closest('details')?.setAttribute('open',''); invalid.reportValidity(); return; }
    const submitted = new Map(controls.map(el => [el, controlValue(el)]));
    let body;
    try { body = payload(); } catch (err) { configFeedback(section, err.message, true); return; }
    section.dataset.saving = 'true'; button.disabled = true;
    const label = button.textContent; button.textContent = '保存中…';
    configFeedback(section, '正在保存这一组配置…');
    try {
      const result = await api(path, {method, body: JSON.stringify(body)});
      if (!section.isConnected) return;
      submitted.forEach((value, el) => configBaselines.set(el, value));
      // A stored credential is no longer needed in the form. Don't clear a new edit made in flight.
      submitted.forEach((value, el) => { if (el.type === 'password' && controlValue(el) === value) { el.value = ''; configBaselines.set(el, ''); el.placeholder = '已配置，留空保留原值'; } });
      configFeedback(section, '已保存；其他分区未保存的修改仍保留。');
      if (after) after(result);
      updateDirtyBadges();
    } catch (err) { if (section.isConnected) configFeedback(section, `保存失败：${err.message}。输入已保留，可修改后重试。`, true); }
    finally { delete section.dataset.saving; button.disabled = false; button.textContent = label; }
  };
}
function updateDirtyBadges() {
  if (!configRoot?.isConnected) return;
  configRoot.querySelectorAll('.card').forEach(card => {
    const dirty = [...card.querySelectorAll('input,select,textarea')].some(el => configBaselines.has(el) && configBaselines.get(el) !== controlValue(el));
    card.classList.toggle('unsaved', dirty);
  });
  const badge = document.getElementById('config-dirty');
  if (badge) badge.textContent = configDirty() ? '有未保存修改' : '配置已同步';
}
function activateConfigSection(id, replace = true) {
  const sections = [...document.querySelectorAll('.config-section')];
  if (!sections.length) return false;
  if (!sections.some(s => s.dataset.section === id)) id = sections[0].dataset.section;
  sections.forEach(el => { el.hidden = el.dataset.section !== id; });
  document.querySelectorAll('[data-config-section]').forEach(a => {
    a.classList.toggle('active', a.dataset.configSection === id);
    if (a.dataset.configSection === id) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  if (replace) { state.route = `${state.page}?section=${encodeURIComponent(id)}`; history.replaceState(null, '', '#/' + state.route); }
  return true;
}
function configWorkspace(groups) {
  const view = document.getElementById('view'); configRoot = view;
  const cards = [...view.querySelectorAll(':scope > .card')];
  const nav = document.createElement('nav'); nav.className = 'config-nav'; nav.setAttribute('aria-label', '配置分类');
  nav.innerHTML = groups.map(g => `<a href="#/${state.page}?section=${g.id}" data-config-section="${g.id}">${esc(g.label)}</a>`).join('') + '<span id="config-dirty" role="status"></span>';
  const body = document.createElement('div'); body.className = 'config-body';
  groups.forEach(g => {
    const section = document.createElement('section'); section.className = 'config-section'; section.dataset.section = g.id;
    cards.filter(card => g.keys.includes(card.dataset.liveKey?.replace(/^card:/,''))).forEach(card => section.append(card));
    body.append(section);
  });
  // Unclassified cards (e.g. demo-mode notice) stay visible rather than being discarded.
  const wrap = document.createElement('div'); wrap.className = 'config-workspace'; wrap.append(nav, body); view.append(wrap);
  nav.addEventListener('click', e => {
    const a = e.target.closest('[data-config-section]');
    if (a && !e.ctrlKey && !e.metaKey && !e.shiftKey) { e.preventDefault(); activateConfigSection(a.dataset.configSection); }
  });
  view.querySelectorAll('input,select,textarea').forEach(el => {
    if (el.readOnly || ['ig-server','ee-server','pb-item'].includes(el.id)) return;
    configBaselines.set(el, controlValue(el));
    el.name ||= el.id;
    if (!el.getAttribute('autocomplete')) el.autocomplete = el.type === 'password' ? 'new-password' : 'off';
    const label = el.closest('.form-row')?.querySelector('label');
    if (label && !label.htmlFor && !label.querySelector('input,select,textarea') && el.id) label.htmlFor = el.id;
    if (!el.labels?.length && !el.getAttribute('aria-label')) el.setAttribute('aria-label', el.placeholder || el.id || '配置值');
  });
  view.oninput = updateDirtyBadges; view.onchange = updateDirtyBadges;
  activateConfigSection(new URLSearchParams((state.route || '').split('?')[1]).get('section'), false);
  updateDirtyBadges();
}
function advancedRows(ids) {
  const rows = ids.map(id => document.getElementById(id)?.closest('.form-row')).filter(Boolean);
  if (!rows.length) return;
  const details = document.createElement('details'); details.className = 'advanced-config';
  const summary = document.createElement('summary'); summary.textContent = '高级参数'; details.append(summary);
  rows[0].before(details); rows.forEach(row => details.append(row));
}
function initSystemSettings() {
  advancedRows(['em-timeout','em-verify']); advancedRows(['pb-direct']);
  configWorkspace([
    {id:'connections',label:'服务连接',keys:['Emby 对接','接入方式','Telegram 机器人']},
    {id:'playback',label:'播放与节点',keys:['播放调度策略','播放分流（Emby 接管）','推流节点']},
    {id:'membership',label:'会员与计费',keys:['会员与计费']},
    {id:'cache',label:'图片缓存',keys:['图片缓存']},
    {id:'entries',label:'外部入口',keys:['外部反代入口']},
  ]);
  configureSave('em-save','/api/settings/emby','PUT',embyPayload);
  configureSave('ig-save','/api/settings/integration','PUT',() => ({panel_public_url:$('#ig-panel').value.trim(),emby_public_url:$('#ig-emby').value.trim(),tmdb_api_key:$('#ig-tmdb').value.trim() || SECRET_KEEP,tmdb_language:$('#ig-tmdb-lang').value.trim() || 'zh-CN'}));
  configureSave('dp-save','/api/settings/dispatch','PUT',() => ({policy:$('#dp-policy').value,load_threshold:Number($('#dp-threshold').value)}));
  configureSave('pb-save','/api/settings/playback','PUT',playbackPayload);
  configureSave('mb-save','/api/settings/membership','PUT',() => ({enforcement_enabled:$('#mb-enforcement').checked,sample_interval_seconds:Number($('#mb-interval').value),retention_days:Number($('#mb-keep').value)}));
  configureSave('ic-save','/api/settings/image-cache','PUT',() => ({enabled:$('#ic-enabled').checked,max_gib:Number($('#ic-gib').value),max_age_days:Number($('#ic-age').value)}),refreshImageCacheStats);
}
function initTelegramSettings(tg) {
  const logoRow = $('#tg-logo').closest('.form-row'), help = logoRow.nextElementSibling, preview = $('#tg-logo-preview-box');
  const box = document.createElement('div'); box.innerHTML = card('首页外观','只改变 Bot 首页图片，不影响注册和群组', '<div class="card-body logo-body"></div>');
  const appearance = box.firstElementChild; appearance.querySelector('.logo-body').append(logoRow,help,preview);
  const save = document.createElement('button'); save.type = 'button'; save.className = 'btn primary'; save.id = 'tg-save-logo'; save.textContent = '保存首页外观'; appearance.querySelector('.logo-body').append(save);
  $('#view').append(appearance);
  const groupBox = document.createElement('div'); groupBox.innerHTML = card('群内交互','换绑只在这里配置的群中审核，群管理员还需具备 Deck 管理员角色', `<div class="card-body"><div class="form-row"><label for="tg-reviewgroups">审核与交互群</label><textarea id="tg-reviewgroups" rows="3" placeholder="-100xxxxxxxxx，每行一个群">${esc((tg.group_interaction_chats || []).join('\n'))}</textarea></div><p class="help">Bot 需要加入这些群。原 TG 失效的用户可用新 TG 验证 Emby 密码申请换绑，无需旧 TG 确认。</p><button class="btn primary" id="tg-save-groups">保存交互群</button></div>`);
  $('#view').append(groupBox.firstElementChild);
  $('#tg-save').textContent = '保存机器人连接'; $('#tg-save2').textContent = '保存注册规则'; $('#tg-test').textContent = '测试已保存连接';
  configWorkspace([{id:'connection',label:'机器人连接',keys:['机器人对接']},{id:'appearance',label:'首页外观',keys:['首页外观']},{id:'registration',label:'注册规则',keys:['注册开户']},{id:'groups',label:'群内交互',keys:['群内交互']},{id:'notifications',label:'通知任务',keys:['通知与排行']}]);
  const payloadKeys = keys => { const all = telegramPagePayload(); return Object.fromEntries(keys.map(k => [k,all[k]])); };
  configureSave('tg-save','/api/settings/telegram','POST',() => payloadKeys(['bot_token','enabled','emby_public_url']));
  configureSave('tg-save2','/api/settings/telegram','POST',() => payloadKeys(['allow_admin_grant','allow_invite','allow_redeem','register_days','max_users','default_group_id','require_group']));
  configureSave('tg-save-logo','/api/settings/telegram','POST',() => ({menu_logo_url:$('#tg-logo').value.trim()}));
  configureSave('tg-save-groups','/api/settings/telegram','POST',() => ({group_interaction_chats:$('#tg-reviewgroups').value.split(/[\n,，]+/).map(x=>x.trim()).filter(Boolean)}));
  $('#tg-test').onclick = async () => {
    const el = $('#tg-result'), button = $('#tg-test'); button.disabled = true; el.textContent = '测试中…';
    try { const r = await api('/api/settings/telegram/verify',{method:'POST'}); if (el.isConnected) el.textContent = r.ok ? `连接正常 @${r.username}；启用状态未改变` : `连接失败：${r.error || '请检查配置'}`; }
    catch (err) { if (el.isConnected) el.textContent = `连接失败：${err.message}`; }
    finally { button.disabled = false; }
  };
}
function setWorkspaceMenu(open, restoreFocus = false) {
  document.body.classList.toggle('nav-open', open);
  document.getElementById('nav-toggle')?.setAttribute('aria-expanded', String(open));
  const backdrop = document.getElementById('nav-backdrop');
  if (backdrop) backdrop.hidden = !open;
  if (restoreFocus) document.getElementById('nav-toggle')?.focus();
}
function installWorkspaceNavigation() {
  const search = document.getElementById('nav-search'), results = document.getElementById('nav-results');
  search.addEventListener('input', () => {
    const query = search.value.trim().toLowerCase(); results.hidden = !query;
    results.innerHTML = query ? NAV.flatMap(g => g.items).filter(it => `${it.label} ${it.sub}`.toLowerCase().includes(query)).map(it => `<a href="#/${it.id}">${esc(it.label)}<small>${esc(it.sub)}</small></a>`).join('') || '<p>没有匹配的页面</p>' : '';
  });
  results.addEventListener('click', () => { results.hidden = true; search.value = ''; });
  document.addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); search.focus(); } if (e.key === 'Escape') { results.hidden = true; if (document.body.classList.contains('nav-open')) setWorkspaceMenu(false, true); } });
  document.getElementById('nav-toggle').onclick = () => setWorkspaceMenu(!document.body.classList.contains('nav-open'));
  document.getElementById('nav-backdrop')?.addEventListener('click', () => setWorkspaceMenu(false, true));
  matchMedia('(max-width:900px)').addEventListener('change', () => setWorkspaceMenu(false));
}
installWorkspaceNavigation();
