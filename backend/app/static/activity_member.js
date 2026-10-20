/* Member-only view: no admin credential, identity or account selector. */
(() => {
  const el = id => document.getElementById(id);
  let current = null;
  let requestId = null;
  async function api(path, body) {
    const r = await fetch('/api/activity/' + path, body === undefined ? {} : {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || '请求失败');
    return data;
  }
  const stamp = value => value ? new Date(value*1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false})+' 北京时间' : '不限期';
  async function load() {
    current = await api('me');
    el('activity-login').hidden = true;
    el('activity-profile').hidden = false;
    const l = current.level, q = current.qualification;
    el('activity-level').textContent = `${l.icon} ${current.username} · Lv${l.level} ${l.name}${current.exempt?' · 白名单':''}`;
    el('activity-title').textContent = current.worn_title || l.title;
    const facts = [
      ['成长', `${l.growth}${l.next_threshold?' / '+l.next_threshold:''}`],
      ['今日静默奖励', `${current.today.points}积分 / ${current.today.growth}成长`],
      ['近30天有效观看', `${Math.floor(q.seconds/60)}/80分钟 · ${q.days}/3合格日`],
      ['续费预算', `${current.renewal_price}积分 · 余额${current.balance} · 缺口${current.shortfall}`],
      ['到期',stamp(current.expires_at)], ['删除候选时间',current.exempt?'白名单豁免':stamp(current.delete_at)],
      ['最终带宽 / 同播',`${current.effective.bandwidth_limit_kbps/1000} Mbps / ${current.effective.max_streams}路`]
    ];
    el('activity-facts').replaceChildren();
    facts.forEach(([key,value]) => {const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=value;el('activity-facts').append(dt,dd);});
    el('activity-auto').checked = current.auto_renew;
    el('activity-auto').disabled = !current.managed || current.exempt;
    el('activity-renew').disabled = !current.managed || current.exempt || !current.window_open;
  }
  async function run(button, fn) {
    button.disabled=true;
    try {await fn(); await load();} catch(e) {el('activity-status').textContent=e.message;} finally {button.disabled=false;}
  }
  el('activity-login-form').onsubmit = e => {e.preventDefault();run(e.submitter, async()=>{await api('login',{code:el('activity-code').value});el('activity-code').value='';el('activity-status').textContent='登录成功';});};
  for (const [id,mode] of [['activity-checkin','normal'],['activity-luck','luck']]) {
    el(id).onclick = () => run(el(id),async()=>{
      if(mode==='luck' && !(await deckConfirm('结果可能更低，覆盖后不可恢复，当天不可重抽。确认运气签到？'))) return;
      const r=await api('checkin',{mode});
      el('activity-status').textContent=r.ok?`今日最终奖励${r.points}积分，本次净调整${r.delta===undefined?r.points:r.delta}，余额${r.balance}`:r.reason;
    });
  }
  el('activity-renew').onclick=()=>run(el('activity-renew'),async()=>{
    if(!(await deckConfirm(`扣除${current.renewal_price}积分续30天？不重置用量、不解除人工限制。`)))return;
    requestId=requestId||crypto.randomUUID();
    const r=await api('renew',{request_id:requestId});
    requestId=null;
    el('activity-status').textContent=`本地已续至${stamp(r.expires_at)}。${r.remote_ok===true?'远端同步已确认':'远端同步未确认，请等待或重试同步；勿重复购买。'}`;
  });
  el('activity-auto').onchange=()=>run(el('activity-auto'),async()=>{await api('auto-renew',{enabled:el('activity-auto').checked});el('activity-status').textContent='自动保号偏好已保存';});
  load().catch(()=>{});
})();
