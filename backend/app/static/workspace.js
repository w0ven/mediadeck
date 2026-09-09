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
  $('#view').insertAdjacentHTML('beforeend', membershipSettingsCards(tg));
  $('#tg-save').textContent = '保存机器人连接'; $('#tg-save2').textContent = '保存注册规则'; $('#tg-test').textContent = '测试已保存连接';
  configWorkspace([{id:'connection',label:'机器人连接',keys:['机器人对接']},{id:'appearance',label:'首页外观',keys:['首页外观']},{id:'registration',label:'注册规则',keys:['注册开户']},{id:'groups',label:'群内交互',keys:['群内交互']},{id:'membership',label:'群组与频道',keys:['群组与频道','定时成员检测']},{id:'notifications',label:'通知任务',keys:['通知与排行']}]);
  const payloadKeys = keys => { const all = telegramPagePayload(); return Object.fromEntries(keys.map(k => [k,all[k]])); };
  configureSave('tg-save','/api/settings/telegram','POST',() => payloadKeys(['bot_token','enabled','emby_public_url']));
  configureSave('tg-save2','/api/settings/telegram','POST',() => payloadKeys(['allow_admin_grant','allow_invite','allow_redeem','register_days','max_users','default_group_id','require_group']));
  configureSave('tg-save-logo','/api/settings/telegram','POST',() => ({menu_logo_url:$('#tg-logo').value.trim()}));
  configureSave('tg-save-groups','/api/settings/telegram','POST',() => ({group_interaction_chats:$('#tg-reviewgroups').value.split(/[\n,，]+/).map(x=>x.trim()).filter(Boolean)}));
  initMembershipSettings(tg);
  $('#tg-test').onclick = async () => {
    const el = $('#tg-result'), button = $('#tg-test'); button.disabled = true; el.textContent = '测试中…';
    try { const r = await api('/api/settings/telegram/verify',{method:'POST'}); if (el.isConnected) el.textContent = r.ok ? `连接正常 @${r.username}；启用状态未改变` : `连接失败：${r.error || '请检查配置'}`; }
    catch (err) { if (el.isConnected) el.textContent = `连接失败：${err.message}`; }
    finally { button.disabled = false; }
  };
}
// Linked membership rules are independent of the interaction/review allowlist.
function membershipSettingsCards(tg) {
  const rules = tg.membership_rules || {targets:[],gate_enabled:false,delete_enabled:false};
  const schedule = tg.membership_schedule || {}, cfg = schedule.config || {};
  return card('群组与频道','所有启用的关联项都必须加入/关注；关联不授予群内发言或审核权限',`<div class="card-body">
    <div class="toolbar"><button class="btn" id="gm-add">＋ 添加关联</button><button class="btn" id="gm-verify">核实名称与权限</button><button class="btn" onclick="go('tggroup')">成员检测 →</button></div>
    <textarea id="gm-targets" hidden aria-label="关联目标配置">${esc(JSON.stringify(rules.targets))}</textarea>
    <div class="table-wrap"><table><thead><tr><th>关联目标 / 实际类型</th><th>加入或关注链接</th><th>Bot权限</th><th>启用 / 操作</th></tr></thead><tbody id="gm-target-rows"></tbody></table></div>
    <p class="help" id="gm-verify-status">Bot须是关联目标的管理员，才能可靠查询并接收成员变动；不会自动升权、生成邀请或踢人。</p>
    <div class="form-row"><label for="gm-gate">使用Bot前校验</label><input id="gm-gate" type="checkbox" ${rules.gate_enabled?'checked':''}><span class="muted">未满足时显示加入群组、关注频道与重新核实按钮；不改Emby直接登录。</span></div>
    <div class="form-row"><label for="gm-delete">退群删除本人账户</label><input id="gm-delete" type="checkbox" ${rules.delete_enabled?'checked':''}><span class="tag warn">敏感操作 · 默认关闭</span></div>
    <p class="help">主动退出、被踢以及手动/定时检测发现存量不合规会员，均在执行前立即再次核实。<b>仅Deck/Emby管理员豁免，白名单同样适用</b>；无宽限，仅删本人Emby＋Deck账号/绑定/设备，不连带邀请人或下级，保留积分、观看及审计历史。查询未知不删除。</p>
    <button class="btn primary" id="gm-save">保存关联规则</button>
  </div>`) + card('定时成员检测','核查Deck已绑定TG会员，不枚举群或频道全员',`<div class="card-body">
    <div class="form-row"><label for="gm-schedule-on">启用定时检测</label><input id="gm-schedule-on" type="checkbox" ${schedule.enabled?'checked':''}></div>
    <div class="form-row"><label for="gm-mode">检测周期</label><select id="gm-mode"><option value="daily" ${cfg.mode==='daily'?'selected':''}>每天</option><option value="interval" ${cfg.mode==='interval'?'selected':''}>固定间隔</option></select></div>
    <div class="form-row"><label for="gm-hour">每天执行时间（服务器时区整点）</label><select id="gm-hour">${Array.from({length:24},(_,h)=>`<option value="${h}" ${Number(cfg.hour??4)===h?'selected':''}>${String(h).padStart(2,'0')}:00</option>`).join('')}</select></div>
    <div class="form-row"><label for="gm-hours">间隔小时</label><input id="gm-hours" type="number" min="1" max="168" value="${esc(cfg.interval_hours??6)}"></div>
    <p class="help">复用任务中心调度。删除开关关闭时只检测；开启时扫描可复核后删除存量不合规本人。无关联时不会执行。<a href="#/tggroup">查看检测进度与最近结果</a></p>
    <div id="gm-schedule-result" class="muted"></div><button class="btn primary" id="gm-schedule-save">保存定时检测</button>
  </div>`);
}
function initMembershipSettings(tg) {
  let targets = JSON.parse($('#gm-targets').value || '[]');
  const ready = () => {
    const active = targets.filter(t=>t.enabled);
    const ok = tg.enabled && active.length && active.every(t=>t.verification==='ready');
    for (const id of ['gm-gate','gm-delete']) { const el=$('#'+id); el.disabled=!el.checked&&!ok; }
  };
  const sync = () => { $('#gm-targets').value=JSON.stringify(targets); ready(); updateDirtyBadges(); };
  const label = t => t.verification==='ready'?'✓ 可核查':t.verification==='permission_unknown'?'权限不足 / 无法确认':'尚未核实 / 查询失败';
  const draw = () => {
    $('#gm-target-rows').innerHTML = targets.map((t,i)=>`<tr data-gm-row="${i}"><td><b class="gm-title">${esc(t.title||'尚未核实名称')}</b><small class="muted gm-type"> ${esc({group:'群组',supergroup:'群组',channel:'频道'}[t.type]||'类型待核实')}</small><input class="gm-id" aria-label="关联目标ID ${i+1}" value="${esc(t.chat_id)}" placeholder="-100… 或 @名称"></td><td><input class="gm-url" aria-label="加入链接 ${i+1}" value="${esc(t.join_url)}" placeholder="https://t.me/…"></td><td class="gm-permission">${esc(label(t))}</td><td><input class="gm-enabled" aria-label="启用关联 ${i+1}" type="checkbox" ${t.enabled?'checked':''}><button class="btn small gm-remove">移除</button></td></tr>`).join('') || '<tr><td colspan="4" class="muted">尚未关联群组或频道；门禁与删除不可启用。</td></tr>';
    // The hidden serialized field is the single dirty baseline for dynamic rows.
    $('#gm-target-rows').querySelectorAll('[data-gm-row]').forEach(row=>{
      const index=Number(row.dataset.gmRow);
      row.oninput=row.onchange=()=>{
        const t=targets[index], next=row.querySelector('.gm-id').value.trim();
        if(next!==t.chat_id){t.title='';t.type='';t.verification='unverified';row.querySelector('.gm-title').textContent='尚未核实名称';row.querySelector('.gm-type').textContent='类型待核实';row.querySelector('.gm-permission').textContent=label(t);}
        t.chat_id=next;t.join_url=row.querySelector('.gm-url').value.trim();t.enabled=row.querySelector('.gm-enabled').checked;sync();
      };
      row.querySelector('.gm-remove').onclick=()=>{targets.splice(index,1);draw();sync();};
    });
    ready();
  };
  draw();
  $('#gm-add').onclick=()=>{targets.push({chat_id:'',join_url:'',enabled:true,title:'',type:'',verification:'unverified'});draw();sync();};
  $('#gm-verify').onclick=async()=>{
    const button=$('#gm-verify'), section=button.closest('.card'), submitted=JSON.stringify(targets);button.disabled=true;
    configFeedback(section,'正在核实关联身份与Bot权限…');
    try{
      const result=await api('/api/telegram/membership/verify',{method:'POST',body:JSON.stringify({targets})});
      if(!section.isConnected)return;
      if(JSON.stringify(targets)===submitted){targets=result.targets;draw();sync();}
      configFeedback(section,result.targets.every(t=>!t.enabled||t.verification==='ready')?'身份与权限已核实；请保存关联规则。':'存在查询失败或权限不足：不能启用门禁/删除。',result.targets.some(t=>t.enabled&&t.verification!=='ready'));
    }catch(err){if(section.isConnected)configFeedback(section,err.message,true);}finally{button.disabled=false;}
  };
  let submitted='';
  configureSave('gm-save','/api/settings/telegram','POST',()=>{submitted=JSON.stringify(targets);return {membership_rules:{targets,gate_enabled:$('#gm-gate').checked,delete_enabled:$('#gm-delete').checked}};},result=>{
    if(JSON.stringify(targets)===submitted){targets=result.membership_rules.targets;draw();$('#gm-targets').value=JSON.stringify(targets);configBaselines.set($('#gm-targets'),$('#gm-targets').value);}
  });
  configureSave('gm-schedule-save','/api/plugins/group_membership','POST',()=>({enabled:$('#gm-schedule-on').checked,config:{mode:$('#gm-mode').value,hour:Number($('#gm-hour').value),interval_hours:Number($('#gm-hours').value)}}));
  const schedule=tg.membership_schedule;
  $('#gm-schedule-result').textContent=schedule?.last_run ? '最近调度结果：'+JSON.stringify(schedule.last_run.summary||{}) : '尚无调度记录（默认关闭）';
}

let workspaceInert = null;
function setWorkspaceMenu(open, restoreFocus = false) {
  const wasOpen = document.body.classList.contains('nav-open');
  open = !!open && matchMedia('(max-width:900px)').matches;
  document.body.classList.toggle('nav-open', open);
  const content = document.getElementById('content');
  if (open && !wasOpen) { workspaceInert = content.inert; content.inert = true; }
  if (!open && wasOpen) { content.inert = workspaceInert; workspaceInert = null; }
  document.getElementById('nav-toggle')?.setAttribute('aria-expanded', String(open));
  const backdrop = document.getElementById('nav-backdrop');
  if (backdrop) backdrop.hidden = !open;
  if (open && !wasOpen) document.querySelector('#nav a')?.focus();
  if (!open && (restoreFocus || (wasOpen && document.activeElement?.closest('#sidebar')))) document.getElementById('nav-toggle')?.focus();
}
function installWorkspaceNavigation() {
  document.querySelector('.skip-link')?.addEventListener('click', e => {
    e.preventDefault(); document.getElementById('view').focus();
  });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Tab' || !document.body.classList.contains('nav-open')) return;
    const links = [...document.querySelectorAll('#nav a')];
    if (e.shiftKey && document.activeElement === links[0]) { e.preventDefault(); links.at(-1)?.focus(); }
    else if (!e.shiftKey && document.activeElement === links.at(-1)) { e.preventDefault(); links[0]?.focus(); }
  });
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

/* Shared identity marks: only the reserved ID receives whitelist decoration. */
function isWhitelistGroup(id) { return id === 'whitelist'; }
function whitelistEmblem() {
  return '<svg class="whitelist-emblem" viewBox="0 0 32 36" fill="none" aria-hidden="true"><path class="wl-shell" d="M16 1.5 29 7v12.5L16 34.5 3 19.5V7Z"/><path class="wl-facet" d="m16 5 9 4.2V18L16 29 7 18V9.2Z"/><path class="wl-crystal" d="m16 9 6 7-6 9-6-9Z"/><path class="wl-spark" d="M16 9v16m-6-9h12M4 5v5M1.5 7.5h5M28 26v5m-2.5-2.5h5"/></svg>';
}
function groupBadge(id, name) {
  return isWhitelistGroup(id) ? `<span class="whitelist-badge">${whitelistEmblem()}<span>${esc(name || '白名单')}</span><small>专属</small></span>` : `<span class="hg-group-badge">${esc(name || '未分组')}</span>`;
}
function workspaceIcon(name) {
  const paths={dashboard:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',members:'<circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6m2 3a5 5 0 0 1 3 4v2"/>',library:'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M7 3v18M17 3v18M3 8h4m-4 8h4M17 8h4m-4 8h4"/>',tgbot:'<rect x="4" y="7" width="16" height="13" rx="4"/><path d="M12 3v4M1 12v4m22-4v4M8 12v2m8-2v2m-7 3h6"/>',nodes:'<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M12 6.5h5m-5 11h5"/>',settings:'<path d="M4 7h16M4 17h16M8 4v6m8 4v6"/>',more:'<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',close:'<path d="m6 6 12 12M6 18 18 6"/>',filter:'<path d="M4 5h16l-6 7v7l-4-2v-5L4 5"/>'};
  return `<svg class="hg-icon" viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.settings}</svg>`;
}

/* Rates are observations, not guesses from media bitrate. One binary byte unit everywhere. */
const RATE_STALE_SECONDS = 15;
function rateMarkup(bps, at, basis, windowSeconds, scope = '') {
  const stamp = Number(at || 0), age = Date.now()/1000 - stamp;
  if (bps == null || !Number.isFinite(Number(bps)) || Number(bps) < 0 || !stamp || age < -5 || age > RATE_STALE_SECONDS) {
    return `<span class="rate-unknown">${stamp ? '采样已过期' : '未实测'}</span>`;
  }
  const details = [scope, windowSeconds ? `约 ${Number(windowSeconds).toFixed(0)} 秒平均` : '采样窗口未提供', basis === 'collector' ? '源采样' : '探针读取（源时间未提供）'].filter(Boolean).join(' · ');
  return `<span class="live-rate" data-rate-at="${stamp}" title="${esc(details)}"><b>${(Number(bps)/1048576).toFixed(2)} MiB/s</b><small>${esc(details)} · <span class="rate-age">${Math.max(0,Math.floor(age))} 秒前</span></small></span>`;
}
function egressValid(n) {
  const stamp = Number(n.egress_sampled_at || n.last_success_ts || n.last_probe_ts || 0);
  return n.ok !== false && n.egress_status !== 'unavailable' && n.egress_mbps != null && Number.isFinite(Number(n.egress_mbps)) && Number(n.egress_mbps) >= 0 && stamp && Date.now()/1000-stamp >= -5 && Date.now()/1000-stamp <= RATE_STALE_SECONDS;
}
function egressCell(n) {
  if (!egressValid(n)) return '<span class="rate-unknown">出口未实测／采样失效</span>';
  return rateMarkup(Number(n.egress_mbps)*1000000/8, n.egress_sampled_at || n.last_success_ts || n.last_probe_ts, n.egress_time_basis, n.egress_window_seconds, '整网卡出口');
}
function egressSummary(nodes) {
  const wanted = nodes.filter(n => n.enabled !== false), known = wanted.filter(egressValid);
  const text = !wanted.length ? '无启用节点' : !known.length ? '暂无有效实测' : rateMarkup(known.reduce((a,n)=>a+Number(n.egress_mbps)*1000000/8,0), Math.min(...known.map(n=>Number(n.egress_sampled_at || n.last_success_ts || n.last_probe_ts))), known.every(n=>n.egress_time_basis==='collector') ? 'collector' : 'probe_received', null);
  return {text, sub: `启用节点整网卡 · ${known.length}/${wanted.length} 有效${known.length < wanted.length ? ' · 仅已知部分' : ''}；不等于播放之和`};
}
function ageLiveRates() {
  document.querySelectorAll('.live-rate[data-rate-at]').forEach(el => {
    const age = Math.max(0, Math.floor(Date.now()/1000-Number(el.dataset.rateAt)));
    if (age > RATE_STALE_SECONDS) { el.textContent = '采样已过期'; el.classList.add('rate-unknown'); }
    else { const label = el.querySelector('.rate-age'); if (label) label.textContent = `${age} 秒前`; }
  });
}
setInterval(ageLiveRates, 1000);
// Keep loaded artwork/failure fallback on SSE patches; a changed artwork key gets a new image.
document.addEventListener('load', e => {
  const img = e.target;
  if (!(img instanceof HTMLImageElement) || !img.classList.contains('pc-art-image')) return;
  img.closest('.pc-art').classList.add('loaded');
  if (img.naturalWidth > img.naturalHeight) img.classList.add('landscape');
}, true);
document.addEventListener('error', e => {
  const img = e.target;
  if (!(img instanceof HTMLImageElement) || !img.classList.contains('pc-art-image')) return;
  img.hidden = true; img.closest('.pc-art').classList.add('failed');
}, true);
