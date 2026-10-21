/* Level editing stays inside the existing groups workspace and Hard Glass controls. */
const activityBeijingStamp = value => new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+' 北京时间';
const legacyGroupsPage = PAGES.groups;
PAGES.groups = async (context = pageContext('groups')) => {
  await legacyGroupsPage(context);
  if (!context.isCurrent()) return;
  const view = document.querySelector('#view');
  const whitelist = view.querySelector('.hg-whitelist-group');
  const compatibility = document.createElement('details');
  compatibility.id='level-legacy-groups';
  compatibility.className='card';
  compatibility.innerHTML='<summary>兼容维护 · 旧用户组（非等级配置）</summary><p class="help">仅维护未迁移账号、旧卡模板及下载/转码等保留权限。普通等级的带宽、同播与求片额度以上方等级配置为准；此处修改不会覆盖等级基础权益。</p>';
  while(view.firstChild) compatibility.append(view.firstChild);
  view.append(compatibility);
  if(whitelist) view.prepend(whitelist);
  const special=document.createElement('section');
  special.className='card';special.id='level-special-rights';
  special.innerHTML='<div class="card-body"><h3>白名单与特殊权益</h3><p>已有与未来销售白名单保留原豁免。永久卡、有效限时卡及个人覆盖仍优先，升级不会清除。专属Lv8仅在成员详情明确授予，不参与自然升级。</p><a class="btn" href="#/members">成员个人权益</a> <a class="btn" href="#/automation?category=points">白名单卡与道具设置</a></div>';
  view.insertBefore(special,compatibility);
  const data = await api('/api/activity/config');
  if (!context.isCurrent()) return;
  const host = document.createElement('section');
  host.className='card';host.id='activity-level-admin';
  const keys = [['name','名称'],['title','称号'],['icon','图标'],['threshold','成长门槛'],['price','30天积分'],['bandwidth_limit_kbps','带宽kbps'],['max_streams','同播'],['request_quota','月求片']];
  host.innerHTML=`<div class="card-head"><h3>等级与权益</h3><span>版本 ${data.revision}</span></div><div class="card-body">
    <p class="help">Lv1～7按独立成长值升级，Lv8仅迁移或管理员明确授予。升级不清个人覆盖/道具，白名单仍保留原豁免与销售。</p>
    <label><input type="checkbox" id="level-enabled" ${data.config.enabled?'checked':''}>启用等级基础权益（迁移须先预览确认）</label>
    <label><input type="checkbox" id="level-delete-enabled" ${data.config.delete_enabled?'checked':''}>启用到期72小时真实删除（默认关闭，须另获授权）</label>
    <div class="table-wrap" style="overflow-x:auto;max-width:100%"><table style="min-width:1100px"><thead><tr><th>等级</th>${keys.map(([,name])=>`<th>${esc(name)}</th>`).join('')}</tr></thead><tbody>${data.config.levels.map((l,i)=>`<tr><td>Lv${l.level}</td>${keys.map(([key])=>`<td><input aria-label="Lv${l.level} ${key}" data-level-row="${i}" data-level-key="${key}" value="${esc(l[key]===null?'专属':l[key])}" ${key==='threshold'&&i===7?'disabled':''} ${typeof l[key]==='number'?'type="number" min="0"':'type="text"'} style="min-width:90px;width:100%"></td>`).join('')}</tr>`).join('')}</tbody></table></div>
    <div class="toolbar"><button id="level-save" class="btn primary">保存等级配置</button><button id="level-preview" class="btn">迁移预览</button><button id="level-delete-preview" class="btn">删除候选预览</button></div><div id="level-feedback" role="status"></div><div id="level-preview-results"></div></div>`;
  document.querySelector('#view').prepend(host);
  document.querySelector('#level-save').onclick=async()=>{
    const config=JSON.parse(JSON.stringify(data.config));
    config.enabled=document.querySelector('#level-enabled').checked;
    config.delete_enabled=document.querySelector('#level-delete-enabled').checked;
    host.querySelectorAll('[data-level-key]:not(:disabled)').forEach(input=>{const row=config.levels[Number(input.dataset.levelRow)],key=input.dataset.levelKey;row[key]=typeof row[key]==='number'?Number(input.value):input.value;});
    if(config.delete_enabled && !data.config.delete_enabled && !(await deckConfirm('真实删除将移除到期72小时的账号。请仅在另行获准后开启，确认启用？',{title:'启用到期真实删除',confirmText:'启用',danger:true}))) return;
    try {const saved=await api('/api/activity/config',{method:'PUT',body:JSON.stringify({config,revision:data.revision})});data.revision=saved.revision;data.config=saved.config;document.querySelector('#level-feedback').textContent='等级配置已保存';}
    catch(e){document.querySelector('#level-feedback').textContent=e.message;}
  };
  document.querySelector('#level-preview').onclick=async()=>{
    try {
      const preview=await api('/api/activity/migration');
      if(!context.isCurrent())return;
      const root=document.querySelector('#level-preview-results');
      root.innerHTML=`<h4>迁移预览（不会改账号）</h4><p>${esc(preview.coverage_note)} · 严格核验覆盖整个窗口：${preview.coverage_complete?'是':'否'}</p><p>迁移窗口：${esc(activityBeijingStamp(preview.window_since))} 至 ${esc(activityBeijingStamp(preview.as_of))}；核验切点：${esc(activityBeijingStamp(preview.verified_since))}（${esc(preview.cutoff_source)}）</p><p>所有普通老用户免费统一30天；人工禁用保留，明确特殊长期限列异常，不静默缩短。</p><div class="table-wrap" style="overflow-x:auto;max-width:100%"><table style="min-width:1100px"><thead><tr><th>账号</th><th>结果</th><th>核验</th><th>期限</th><th>异常</th></tr></thead><tbody>${preview.members.map(r=>`<tr><td>${esc(r.username)}</td><td>Lv${r.level} / ${esc(r.action)}</td><td>${Math.floor(r.qualification.seconds/60)}分钟 / ${r.qualification.days}天<br>旧采样${Math.floor(r.qualification.sources.watch_samples)}秒 · 核验${Math.floor(r.qualification.sources.watch_verified_samples)}秒</td><td>${r.new_expires_at?esc(activityBeijingStamp(r.new_expires_at)):'不限期'}</td><td>${esc(r.reason)}</td></tr>`).join('')}</tbody></table></div><button class="btn danger" id="level-migrate-confirm">确认本次迁移</button>`;
      const requestId=crypto.randomUUID();
      document.querySelector('#level-migrate-confirm').onclick=async()=>{
        if(!(await deckConfirm('确认按此预览执行一次性迁移？仅应在已获迁移授权的环境操作。',{title:'执行等级迁移',confirmText:'执行迁移',danger:true})))return;
        try{const r=await api('/api/activity/migration',{method:'POST',body:JSON.stringify({confirm:true,request_id:requestId,hash:preview.hash,as_of:preview.as_of})});document.querySelector('#level-feedback').textContent=`迁移完成 ${r.migrated.length} 人`;document.querySelector('#level-migrate-confirm').disabled=true;}
        catch(e){document.querySelector('#level-feedback').textContent=e.message;}
      };
    }catch(e){document.querySelector('#level-feedback').textContent=e.message;}
  };
  document.querySelector('#level-delete-preview').onclick=async()=>{
    try {const rows=await api('/api/activity/deletion-preview');document.querySelector('#level-preview-results').textContent=rows.length?rows.map(r=>`${r.username} · ${activityBeijingStamp(r.delete_at)} · ${r.reason||'到期候选'}`).join('\n'):'没有删除候选；预览没有执行删除。';}
    catch(e){document.querySelector('#level-feedback').textContent=e.message;}
  };
};
