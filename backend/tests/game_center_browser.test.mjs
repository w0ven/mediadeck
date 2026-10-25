/*
 * Chromium + shipped Mini App + isolated real FastAPI/SQLite.
 * CI/local pytest: python -m pytest tests/test_game_center_browser.py -q
 * Direct local run: HOME=/root MEDIADECK_MOCK=1 TZ=Asia/Shanghai node --test tests/game_center_browser.test.mjs
 * No Python files or running services are changed. Python's installed Playwright supplies
 * its bundled Node driver. A stdio TestClient bridge follows test_economy_browser.py.
 * Native EventSource receives the actual ASGI stream's first frame; transport EOF is
 * intentional, exercising token renewal/full-snapshot reconnect without a daemon.
 * GAME_CENTER_UI_ARTIFACTS optionally saves viewport/theme matrices and real table sheets.
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { spawn, execFileSync } from 'node:child_process';
import { createInterface } from 'node:readline';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import fs from 'node:fs/promises';
const backend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
// pytest supplies its own interpreter; CI installs globally rather than in .venv.
const python = process.env.MEDIADECK_TEST_PYTHON || path.join(backend, '.venv/bin/python');
const require = createRequire(import.meta.url);
const driver = execFileSync(python, ['-c', 'import pathlib,playwright; print(pathlib.Path(playwright.__file__).parent / "driver" / "package")'], { encoding: 'utf8' }).trim();
const { chromium } = require(driver);
const ORIGIN = 'https://app.example.test';
const ROOT = '/api/app/games';
const output = process.env.GAME_CENTER_UI_ARTIFACTS;
const PYTHON_BRIDGE = String.raw`
import asyncio, base64, contextlib, json, os, sys, tempfile, traceback
import pytest
from random import Random
from datetime import datetime
# Capture only protocol messages on stdout; application loggers use stderr.
protocol = sys.stdout
sys.stdout = sys.stderr
with tempfile.TemporaryDirectory(prefix='gc-browser-') as directory:
    os.environ.update(MEDIADECK_DATA_DIR=directory, MEDIADECK_MOCK='1', MEDIADECK_ADMIN_USER='admin', MEDIADECK_ADMIN_PASSWORD='change-me')
    from app.core.config import settings
    settings.cache_clear()
    from app.main import app
    from tests.test_miniapp import client as fixture, sign, TOKEN, TELEGRAM_STUB
    from app.modules import game_center_zjh
    from app.modules.game_center_config import DEFAULTS
    patch = pytest.MonkeyPatch()
    fixture_gen = fixture.__wrapped__(patch)
    client = next(fixture_gen)
    svc = app.state.game_center
    client.portal.call(app.state.game_center_task.cancel)
    client.portal.call(app.state.game_delivery_task.cancel)
    app.state.plugins.save('game_center', enabled=True, config={**DEFAULTS, 'zjh_enabled': True, 'holdem_enabled': True, 'xiangqi_enabled': True, 'gomoku_enabled': True})
    # Keep deadline advances away from midnight's intentional daily-bean refill.
    clock = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    svc._clock = lambda value=None: clock if value is None else float(value)
    front = [12,25,5,11,24,4,10,23,3,9,22,2,8,21,1]
    class FixedRandom(Random):
        def __init__(self):
            super().__init__(17)
        def shuffle(self, values):
            values[:] = front + [n for n in range(52) if n not in front]
    patch.setattr(game_center_zjh.secrets, 'SystemRandom', FixedRandom)
    for index in range(1, 7):
        uid = 'u' + str(index)
        if index > 2:
            app.state.members.upsert(uid, 'browser-member-' + str(index), {'group_id': 'standard'})
        app.state.members.bind_telegram(uid, str(777000000 + index * 111))
        if index != 1:
            app.state.points.add(uid, 3500, 'browser.fixture')
    users = {i: sign({'id':777000000+i*111, 'first_name':['','可乐','小雨','阿杰','小明','糖糖','阿飞'][i]}) for i in range(1,7)}
    async def first_frame(url):
        from urllib.parse import urlsplit
        u = urlsplit(url)
        disconnected = asyncio.Event()
        first = True
        result = {'status': 200, 'headers': {}, 'chunks': []}
        async def receive():
            nonlocal first
            if first:
                first = False
                return {'type':'http.request', 'body':b'', 'more_body':False}
            await disconnected.wait()
            return {'type':'http.disconnect'}
        async def send(message):
            if message['type'] == 'http.response.start':
                result['status'] = message['status']
                result['headers'] = {k.decode():v.decode() for k,v in message['headers']}
            elif message['type'] == 'http.response.body':
                result['chunks'].append(message.get('body', b''))
                if b'event: snapshot' in message.get('body', b'') or not message.get('more_body'):
                    disconnected.set()
        scope = {'type':'http','asgi':{'version':'3.0','spec_version':'2.0'},'http_version':'1.1','scheme':'https',
                 'method':'GET','path':u.path,'raw_path':u.path.encode(),'query_string':u.query.encode(),
                 'root_path':'','headers':[(b'host', b'app.example.test')], 'client':('testclient',50000),
                 'server':('app.example.test',443)}
        await asyncio.wait_for(app(scope, receive, send), 5)
        return {'status':result['status'], 'headers':result['headers'], 'body':base64.b64encode(b''.join(result['chunks'])).decode()}
    try:
        for line in sys.stdin:
            message = json.loads(line)
            try:
                kind = message.get('kind')
                if kind == 'init':
                    result = {'init':users, 'stub':TELEGRAM_STUB}
                elif kind == 'advance':
                    raw = svc._load(app.state.db._conn, message['table'])
                    if message.get('deadline'):
                        key = {'countdown': 'countdown_at', 'turn': 'turn_deadline', 'auto': 'auto_at'}[message['deadline']]
                        clock = raw[key] + .02
                    else:
                        clock += message.get('seconds', 0)
                    svc.tick(now=clock, table_id=message['table'])
                    result = svc.snapshot(message['table'], 'u'+str(message.get('user',1)))
                elif kind == 'deck':
                    # Deterministic shuffle input only; all dealing, actions and settlement stay real.
                    front = message['front']
                    assert len(front) == len(set(front)) and all(0 <= n < 52 for n in front)
                    result = True
                elif kind == 'balance':
                    uid = 'u'+str(message['user'])
                    app.state.points.add(uid, message['amount']-app.state.points.balance(uid), 'browser.fixture')
                    result = svc.wallet(uid)
                elif kind == 'sse':
                    result = client.portal.call(first_frame, message['path'])
                else:
                    headers = message.get('headers', {})
                    if message.get('user'):
                        headers['X-Telegram-Init-Data'] = users[message['user']]
                    response = client.request(message.get('method','GET'), message['path'], auth=None,
                        headers=headers, content=message.get('body'))
                    result = {'status':response.status_code, 'headers':dict(response.headers),
                              'body':base64.b64encode(response.content).decode()}
                protocol.write(json.dumps({'id':message['id'],'result':result})+'\n'); protocol.flush()
            except Exception:
                protocol.write(json.dumps({'id':message['id'],'error':traceback.format_exc()})+'\n'); protocol.flush()
    finally:
        with contextlib.suppress(StopIteration): next(fixture_gen)
        patch.undo()
        settings.cache_clear()
`;

async function environment() {
  const proc = spawn(python, ['-u', '-c', PYTHON_BRIDGE], { cwd: backend, env: { ...process.env, MEDIADECK_MOCK: '1', TZ: 'Asia/Shanghai' }, stdio: ['pipe','pipe','pipe'] });
  let sequence = 0, stderr = '';
  const pending = new Map();
  proc.stderr.on('data', d => { stderr = (stderr + d).slice(-10000); });
  createInterface({ input: proc.stdout }).on('line', line => {
    const data = JSON.parse(line), p = pending.get(data.id);
    if (!p) return;
    pending.delete(data.id);
    data.error ? p.reject(new Error(data.error)) : p.resolve(data.result);
  });
  proc.on('exit', code => { for (const p of pending.values()) p.reject(new Error(`bridge exited ${code}: ${stderr}`)); });
  function call(data) {
    return new Promise((resolve, reject) => { const id = ++sequence; pending.set(id, { resolve, reject }); proc.stdin.write(JSON.stringify({ ...data, id }) + '\n'); });
  }
  const init = await call({ kind: 'init' });
  const browser = await chromium.launch({ executablePath: process.env.MEDIADECK_TEST_CHROMIUM || '/usr/bin/chromium', args: ['--no-sandbox'], headless: true });
  const errors = [], requests = [], pages = [];
  let transportFailures = 0, loseResponse = null, simulated = null;
  async function request(user, pathname, method = 'GET', body) {
    const response = await call({ user, path: ROOT + pathname, method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify({ ...body, request_id: body.request_id || 'test_' + crypto.randomUUID() }) : undefined });
    const data = JSON.parse(Buffer.from(response.body, 'base64').toString());
    assert.equal(response.status, 200, JSON.stringify(data));
    return data;
  }
  async function pageFor(user = 1, theme = 'dark', width = 400, height = 860, controlledStream = false, motion = false) {
    const context = await browser.newContext({ viewport: { width, height }, reducedMotion: motion ? 'no-preference' : 'reduce' });
    const page = await context.newPage(); pages.push(page);
    page.setDefaultTimeout(12000);
    page.on('pageerror', e => errors.push(String(e)));
    await page.addInitScript(controlled => {
      window.__streams = [];
      // Native streams exercise transport above; this option isolates timing and expiry below.
      const Native = controlled ? class extends EventTarget { close() {} } : window.EventSource;
      window.EventSource = class extends Native {
        constructor(url) { super(url); this.testClosed = false; window.__streams.push(this); }
        close() { this.testClosed = true; super.close(); }
      };
    }, controlledStream);
    await page.route('**/*', async route => {
      const r = route.request(), url = new URL(r.url());
      if (url.hostname === 'telegram.org') {
        return route.fulfill({ contentType: 'application/javascript', body: init.stub.replace('%s', JSON.stringify(init.init[user])).replace("colorScheme: 'dark'", `colorScheme: '${theme}'`) });
      }
      if (url.origin !== ORIGIN) return route.abort();
      const pathname = url.pathname + url.search;
      const body = r.postData();
      requests.push({ user, path: url.pathname, method: r.method(), body: body && JSON.parse(body), init: r.headers()['x-telegram-init-data'] });
      if (simulated && url.pathname.startsWith(ROOT)) {
        const result = await simulated({ user, path: url.pathname.slice(ROOT.length), body: body && JSON.parse(body), method: r.method() });
        if (result) return route.fulfill({ status: result.status || 200, contentType: result.contentType || 'application/json', body: typeof result.body === 'string' ? result.body : JSON.stringify(result.body) });
      }
      if (url.pathname.endsWith('/events')) {
        if (transportFailures > 0) { transportFailures--; return route.abort('connectionfailed'); }
        const response = await call({ kind: 'sse', path: pathname });
        return route.fulfill({ status: response.status, contentType: 'text/event-stream', body: Buffer.from(response.body, 'base64') });
      }
      const response = await call({ path: pathname, method: r.method(), body: body || undefined, headers: Object.fromEntries(Object.entries(r.headers()).filter(([k]) => ['content-type', 'x-telegram-init-data'].includes(k))) });
      if (loseResponse && url.pathname.endsWith(loseResponse) && r.method() === 'POST') { loseResponse = null; return route.abort('connectionfailed'); }
      return route.fulfill({ status: response.status, headers: Object.fromEntries(Object.entries(response.headers).filter(([k]) => !['content-length', 'content-encoding'].includes(k))), body: Buffer.from(response.body, 'base64') });
    });
    return page;
  }
  return { browser, call, request, pageFor, requests, errors, pages,
    failStream: () => { transportFailures++; }, loseNext: suffix => { loseResponse = suffix; }, simulate: fn => { simulated = fn; },
    close: async () => { await browser.close(); proc.stdin.end(); await new Promise(resolve => proc.once('exit', resolve)); },
  };
}
const countRequests = (env, path, user = 1) => env.requests.filter(r => r.path.endsWith(path) && r.user === user).length;
const waitRevision = (page, rev) => page.waitForFunction(n => Number(document.querySelector('.gc-table')?.dataset.revision) >= n, rev);
async function closeSheet(page) { if (await page.locator('.gc-sheet').isVisible()) await page.locator('.gc-sheet [data-cancel]').last().click(); }
async function capture(page, name) {
  assert(!/mediadeck/i.test(await page.locator('body').innerText()), 'member surface has no management-panel branding');
  assert(!/mediadeck/i.test(await page.title()), 'member document title has no management-panel branding');
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'no document horizontal overflow');
  assert(await page.evaluate(() => { const s = document.querySelector('#screen'); return s.scrollWidth <= s.clientWidth; }), 'no screen horizontal overflow');
  await page.waitForFunction(() => [...document.querySelectorAll('.gc img')].every(i => i.complete && i.naturalWidth > 0));
  await page.waitForFunction(() => [...document.querySelectorAll('[data-gc-write][data-gc-disabled="false"]')].every(b => !b.disabled));
  if (await page.locator('.gc-board-table').count() && !await page.locator('.gc-sheet').isVisible()) {
    const layout = await page.evaluate(() => {
      const box = s => document.querySelector(s).getBoundingClientRect();
      const frame = box('.gc-board-frame'), board = box('[data-gc-board]');
      const bars = [...document.querySelectorAll('.gc-duel-bar')].map(e => e.getBoundingClientRect());
      return { overlap: bars.some(b => Math.min(b.bottom, frame.bottom) - Math.max(b.top, frame.top) > 1),
        inset: [board.left - frame.left, frame.right - board.right, board.top - frame.top, frame.bottom - board.bottom],
        actionBottom: document.querySelector('.gc-board-dock').getBoundingClientRect().bottom, height: innerHeight };
    });
    assert.equal(layout.overlap, false, 'board frame and player bars must not overlap');
    assert(layout.inset.every(v => v >= 0 && v <= 9), `board sits inside its frame: ${layout.inset}`);
    assert(layout.actionBottom <= layout.height + 1, 'board actions fit the viewport');
  } else if (await page.locator('.gc-table').count() && !await page.locator('.gc-sheet').isVisible()) {
    const layout = await page.evaluate(() => {
      const targets = [...document.querySelectorAll('.gc-croupier,.gc-seat,.gc-holdem-center,.gc-pot,.gc-me-row')];
      const boxes = targets.map(e => ({label:e.className, r:e.getBoundingClientRect()}));
      const collisions = [];
      for (let i=0;i<boxes.length;i++) for (let j=i+1;j<boxes.length;j++) {
        const a=boxes[i].r,b=boxes[j].r;
        if (Math.min(a.right,b.right)-Math.max(a.left,b.left)>1 && Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1) collisions.push([boxes[i].label,boxes[j].label]);
      }
      const dealer = document.querySelector('.gc-croupier').getBoundingClientRect();
      const center = document.querySelector('.gc-holdem-center,.gc-pot').getBoundingClientRect();
      return {collisions, dealerGap: center.top - dealer.bottom, actionBottom:document.querySelector('.gc-actions')?.getBoundingClientRect().bottom || 0, footerBottom:document.querySelector('.gc-turn-line')?.getBoundingClientRect().bottom || 0, height:innerHeight,
        brand:document.querySelector('.gc-table').innerText.includes('MediaDeck') || getComputedStyle(document.querySelector('.gc-oval'),'::after').content.toLowerCase().includes('mediadeck')};
    });
    assert.deepEqual(layout.collisions, [], 'decorative dealer/seats/board/pot must not overlap');
    assert(layout.dealerGap >= 6, `dealer/center gap is at least 6px, got ${layout.dealerGap}`);
    assert(layout.actionBottom <= layout.height + 1, 'actions fit the viewport');
    assert(layout.footerBottom <= layout.height + 1, 'turn timer fits the viewport');
    assert.equal(layout.brand,false,'member surface has no management-panel branding');
  }
  if (output && name) { await fs.mkdir(output, { recursive: true }); await page.screenshot({ path: path.join(output, name), fullPage: true, animations: 'disabled' }); }
}
async function matrix(page, label) {
  for (const [width, height] of [[400,860],[360,740]]) for (const theme of ['dark','light']) {
    await page.setViewportSize({ width, height });
    await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
    await capture(page, label ? `${label}-${width}x${height}-${theme}.png` : null);
  }
  await page.setViewportSize({ width: 400, height: 860 });
  await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
}

test('real ZJH member UI, host/ready/action/auto, deep links, native SSE reconnect and cleanup', { timeout: 180000 }, async () => {
  const env = await environment();
  try {
    const host = await env.pageFor(1), guest = await env.pageFor(2);
    await host.goto(ORIGIN + '/app');
    await host.locator('#tabbar [data-tab="games"]').click();
    await host.locator('.gc-lobby').waitFor();
    await host.locator('#gc-waiting .empty').waitFor();
    assert.equal(await host.locator('.gc-game-card[data-gc-soon]').count(), 2, 'niuniu and doudizhu are still coming soon');
    assert.deepEqual(await host.locator('.gc-board-card .gc-play-tag').allInnerTexts(), ['可玩', '可玩']);
    assert.equal(await host.locator('#i-gamepad-2').count(), 1);
    await matrix(host, '01-lobby');
    await host.locator('[data-gc-tiers="holdem"]').click();
    await host.locator('.gc-tier').first().waitFor();
    assert.equal(await host.locator('.gc-tier').count(), 6);
    assert(await host.locator('[data-gc-tier="supreme"]').innerText().then(s => s.includes('不设上限')));
    assert(await host.locator('[data-gc-tier="novice"] [data-gc-enter="quick"]').isEnabled());
    await host.locator('[data-gc-back]').click();
    await host.locator('[data-gc-tiers="zjh"]').click();
    await host.locator('.gc-tier').first().waitFor();
    await matrix(host, '02-zjh-tiers');
    await host.locator('[data-gc-tier="novice"] [data-gc-enter="create"]').click();
    await host.locator('[data-gc-confirm]').click();
    await host.locator('.gc-table').waitFor();
    const id = await host.locator('.gc-table').getAttribute('data-table-id');
    let t = (await env.request(1, '/tables/' + id)).table;
    assert.equal(t.me.is_host, true); assert.equal(t.me.wallet.frozen, 0);
    assert(await host.locator('[data-gc-start]').isDisabled());
    assert.match(await host.locator('.gc-wait-reason').innerText(), /还差 1 人入座/);
    await matrix(host, '02b-zjh-waiting'); // Waiting text also stays below the dealer at both sizes/themes.
    // Opening a deep link never seats/freezes; explicit click does.
    await guest.goto(ORIGIN + '/app?tgWebAppStartParam=gc_' + id);
    await guest.locator('[data-gc-seat]').waitFor();
    assert.equal((await env.request(2, '/tables/' + id)).table.me.seat, null);
    assert.equal(env.requests.filter(r => r.user === 2 && r.path.endsWith('/seat')).length, 0);
    await guest.locator('[data-gc-seat]').click();
    await guest.locator('[data-gc-ready]').waitFor();
    assert.equal((await env.request(2, '/wallet')).frozen, 0);
    // Lost ready response: retry must use the original request_id.
    env.loseNext('/ready');
    await guest.locator('[data-gc-ready]').click();
    await guest.locator('[data-gc-pending]').waitFor();
    await guest.locator('[data-gc-pending]').click();
    await guest.waitForFunction(() => document.querySelector('[data-gc-ready]')?.textContent.includes('取消准备'));
    const readyCalls = env.requests.filter(r => r.user === 2 && r.path.endsWith('/ready'));
    assert.equal(readyCalls.at(-1).body.request_id, readyCalls.at(-2).body.request_id);
    t = (await env.request(1, '/tables/' + id)).table;
    await waitRevision(host, t.revision);
    assert(!(await host.locator('[data-gc-start]').isDisabled()));
    await host.locator('[data-gc-ready]').click();
    await host.locator('[data-gc-countdown]').first().waitFor();
    assert.equal((await env.request(1, '/tables/' + id)).table.phase, 'waiting');
    await host.locator('[data-gc-ready]').click();
    await host.waitForFunction(() => !document.querySelector('[data-gc-countdown]'));
    // Host starts with ready guests even though host didn't separately ready.
    await host.locator('[data-gc-start]').click();
    await host.waitForFunction(() => document.querySelector('.gc-table')?.dataset.phase === 'playing');
    t = (await env.request(1, '/tables/' + id)).table;
    assert.equal(t.me.wallet.frozen, 49); // 50 reserved minus the 1-point ante already in the pot.
    const turn = t.hand.turn, deadline = t.turn_deadline;
    const nonactor = t.hand.current_player === t.me.player ? guest : host;
    const nonactorUser = nonactor === guest ? 2 : 1;
    const otherUser = nonactorUser === 1 ? 2 : 1;
    await nonactor.locator('.gc-hand[data-gc-action="look"]').waitFor();
    await nonactor.locator('.gc-hand[data-gc-action="look"]').click();
    const view = (await env.request(nonactorUser, '/tables/' + id)).table;
    assert.equal(view.hand.turn, turn); assert.equal(view.turn_deadline, deadline);
    assert.equal(view.hand.players.find(p => p.player === view.me.player).cards.length, 3);
    const other = (await env.request(otherUser, '/tables/' + id)).table;
    assert.equal(other.hand.players.find(p => p.player === view.me.player).cards, null);
    if (await host.locator('.gc-hand[data-gc-action="look"]').count()) await host.locator('.gc-hand[data-gc-action="look"]').click();
    await host.waitForFunction(() => document.querySelectorAll('.gc-hand .gc-card:not(.gc-card-back)').length === 3);
    await matrix(host, '03-zjh-table');
    // At least one real betting action, carrying exact server round + intent id.
    t = (await env.request(1, '/tables/' + id)).table;
    const actorPage = t.hand.current_player === t.me.player ? host : guest;
    await actorPage.locator('[data-gc-action="call"]').click();
    const actionCall = env.requests.filter(r => r.path.endsWith('/actions') && r.body.action === 'call').at(-1);
    assert(Number.isInteger(actionCall.body.hand_no) && Number.isInteger(actionCall.body.turn));
    assert.match(actionCall.body.request_id, /^[\w-]{8,80}$/);
    await host.locator('[data-gc-auto]').click();
    await host.waitForFunction(() => document.querySelector('[data-gc-auto]')?.getAttribute('aria-checked') === 'true');
    assert.match(await host.locator('.gc-me-row').innerText(), /托管中/);
    await host.locator('[data-gc-auto]').click();
    await host.waitForFunction(() => document.querySelector('[data-gc-auto]')?.getAttribute('aria-checked') === 'false');
    t = (await env.request(1, '/tables/' + id)).table;
    const timeoutPage = t.hand.current_player === t.me.player ? host : guest;
    const timeoutUser = timeoutPage === host ? 1 : 2;
    await env.call({ kind: 'advance', table: id, deadline: 'turn', user: timeoutUser });
    await timeoutPage.locator('[data-gc-takeover-sheet]').waitFor();
    assert.match(await timeoutPage.locator('.gc-timeout').innerText(), /超时/);
    await capture(timeoutPage, '05-zjh-timeout.png');
    await timeoutPage.locator('[data-gc-takeover-sheet]').click();
    await timeoutPage.waitForFunction(() => document.querySelector('[data-gc-auto]')?.getAttribute('aria-checked') === 'false');
    // Faulted SSE transport must re-sign and fetch a fresh snapshot, not replay old actions.
    const beforeToken = countRequests(env, '/stream-token'), beforeGet = countRequests(env, '/tables/' + id);
    env.failStream();
    await host.locator('[data-gc-connection]').click();
    await host.waitForFunction(() => window.__streams.length >= 2);
    for (let i = 0; i < 80 && (countRequests(env, '/stream-token') <= beforeToken + 1 || countRequests(env, '/tables/' + id) <= beforeGet + 1); i++) await host.waitForTimeout(80);
    assert(countRequests(env, '/stream-token') > beforeToken + 1);
    assert(countRequests(env, '/tables/' + id) > beforeGet + 1);
    // Backgrounded Mini Apps close the stream; visibility restoration pulls a new snapshot.
    await host.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => true }); document.dispatchEvent(new Event('visibilitychange')); });
    assert(await host.evaluate(() => window.__streams.every(s => s.testClosed)));
    const hiddenTokens = countRequests(env, '/stream-token');
    await host.waitForTimeout(900);
    assert.equal(countRequests(env, '/stream-token'), hiddenTokens);
    await host.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => false }); document.dispatchEvent(new Event('visibilitychange')); });
    // Reopening ordinary Mini App (no link) restores the server's active table.
    await host.reload();
    await host.locator('.gc-table').waitFor();
    assert.equal(await host.locator('.gc-table').getAttribute('data-table-id'), id);
    t = (await env.request(1, '/tables/' + id)).table;
    const finisher = t.hand.current_player === t.me.player ? host : guest;
    await closeSheet(finisher);
    await finisher.locator('[data-gc-action="fold"]').click();
    t = (await env.request(1, '/tables/' + id)).table;
    assert.equal(t.phase, 'waiting'); assert.equal(t.me.wallet.frozen, 0);
    await waitRevision(host, t.revision);
    await closeSheet(host);
    await host.locator('[data-gc-history]').first().click();
    assert.match(await host.locator('.gc-result-head').innerText(), /结算/);
    assert.equal(await host.locator('[data-gc-result-verify]').count(), 0);
    await capture(host, '06-zjh-result.png');
    await closeSheet(host);
    // Leaving the game page closes every native stream and stops reconnect timers.
    await host.locator('[data-gc-back]').click();
    await host.locator('.gc-lobby').waitFor();
    assert(await host.evaluate(() => window.__streams.every(s => s.testClosed)));
    const stopped = countRequests(env, '/stream-token');
    await host.waitForTimeout(1000);
    assert.equal(countRequests(env, '/stream-token'), stopped);
    // Privacy + auth: every ordinary game API request has signed initData; SSE has only token.
    assert(env.requests.filter(r => r.path.startsWith(ROOT) && !r.path.endsWith('/events')).every(r => r.init));
    assert.equal(env.errors.length, 0, env.errors.join('\n'));
    // Low balance, supreme explanation and practice currency remain real API states.
    const poor = await env.pageFor(3);
    await env.call({ kind: 'balance', user: 3, amount: 20 });
    await poor.goto(ORIGIN + '/app?tgWebAppStartParam=games');
    await poor.locator('[data-gc-tiers="zjh"]').click();
    await poor.locator('.gc-tier').first().waitFor();
    assert(await poor.locator('[data-gc-tier="novice"] [data-gc-enter="quick"]').isDisabled());
    assert.match(await poor.locator('[data-gc-tier="supreme"]').innerText(), /单局不封顶/);
    await poor.locator('[data-gc-tier="practice"] [data-gc-enter="quick"]').click();
    await poor.locator('[data-gc-confirm]').click();
    await poor.locator('.gc-table').waitFor();
    assert.equal(await poor.locator('.gc-bot-label').count(), 4);
    assert.match(await poor.locator('.gc-me-row').innerText(), /欢乐豆/);
    await poor.locator('[data-gc-start]').click();
    await poor.waitForFunction(() => document.querySelector('.gc-table')?.dataset.phase === 'playing');
    for (const [width,height] of [[400,860],[360,740]]) {
      await poor.setViewportSize({width,height});
      await capture(poor,null); // all five occupied ZJH seats, including the decorative dealer fallback
    }
    assert.equal(env.errors.length, 0, env.errors.join('\n'));
  } finally { await env.close(); }
});

// All Hold'em HTTP requests and native SSE frames below come from FastAPI/SQLite.
// Only the clock and shuffle input are controlled, just like the backend integration tests.
const tableFor = async (env, id, user = 1) => (await env.request(user, '/tables/' + id)).table;
async function syncTable(page, table) {
  await closeSheet(page);
  await page.locator('[data-gc-connection]').click();
  await waitRevision(page, table.revision);
}
async function shots(page, label) {
  for (const [width, height] of [[400, 860], [360, 740]]) {
    await page.setViewportSize({ width, height });
    await capture(page, label ? `${label}-${width}x${height}.png` : null);
  }
  await page.setViewportSize({ width: 400, height: 860 });
}
async function holdemPage(env, pages, id, user) {
  if (!pages.has(user)) {
    const page = await env.pageFor(user);
    pages.set(user, page);
    await page.goto(`${ORIGIN}/app?tgWebAppStartParam=gc_${id}`);
    await page.locator('.gc-holdem').waitFor();
  }
  return pages.get(user);
}
async function holdemAction(env, pages, id, action, amount) {
  let table = await tableFor(env, id);
  // These fixtures seat user N in seat N-1, through the real seating endpoint.
  const user = table.seats.find(s => s.player === table.hand.current_player).seat + 1;
  table = await tableFor(env, id, user);
  const page = await holdemPage(env, pages, id, user);
  await syncTable(page, table);
  const legal = table.hand.legal_actions;
  const expected = ['fold', 'check'].filter(key => legal[key]);
  if (legal.call > 0) expected.push('call');
  if (legal.min_raise_to != null) expected.push(table.hand.engine.current_bet ? 'raise' : 'bet');
  if (legal.all_in > 0) expected.push('all_in');
  const actual = await page.locator('[data-gc-action]').evaluateAll(nodes => nodes.map(n => n.dataset.gcAction));
  assert.deepEqual(actual.sort(), expected.sort(), 'buttons match this viewer’s engine legal actions exactly');
  action ||= legal.check ? 'check' : 'call';
  await page.locator(`[data-gc-action="${action}"]`).click();
  if (['bet', 'raise'].includes(action)) {
    if (amount === 'half') await page.locator('[data-gc-size="0.5"]').click();
    else await page.locator('.gc-sheet .gc-range').evaluate((el, value) => {
      el.value = String(value); el.dispatchEvent(new Event('input', { bubbles: true }));
    }, amount ?? legal.min_raise_to);
    await page.locator('[data-gc-raise-confirm]').click();
  } else if (action === 'all_in') await page.locator('[data-gc-confirm]').click();
  await waitRevision(page, table.revision + 1);
  const sent = env.requests.filter(r => r.user === user && r.path.endsWith('/actions')).at(-1).body;
  assert.equal(sent.hand_no, table.hand_no);
  assert.equal(sent.turn, table.hand.turn);
  assert.equal(sent.action, action);
  assert.match(sent.request_id, /^[\w-]{8,80}$/);
  if (!['bet', 'raise'].includes(action)) assert.equal(sent.amount, undefined);
  return tableFor(env, id);
}
async function finishHoldem(env, pages, id) {
  for (let i = 0; i < 100; i++) {
    const table = await tableFor(env, id);
    if (!table.hand) return table;
    await holdemAction(env, pages, id);
  }
  assert.fail('real Hold’em hand did not settle');
}
async function createHoldem(env, amounts, tier = 'normal') {
  const { table } = await env.request(1, '/tables', 'POST', { game: 'holdem', tier, buy_in: amounts[0] });
  for (let user = 2; user <= amounts.length; user++) {
    await env.request(user, `/tables/${table.id}/seat`, 'POST', { buy_in: amounts[user - 1] });
  }
  return tableFor(env, table.id);
}

test('real Holdem: buy-in, quick/deep-link join, host start, continuous hands, betting, top-up, away, timeout and cash-out', { timeout: 120000 }, async () => {
  const env = await environment();
  try {
    const host = await env.pageFor(1), guest = await env.pageFor(2), quick = await env.pageFor(3);
    const pages = new Map([[1, host], [2, guest], [3, quick]]);
    // An intentionally skewed device clock must still show the server's four-second countdown.
    await host.clock.install({ time: new Date('2030-01-01T00:00:00Z') });
    const initialWallet = await env.request(1, '/wallet');
    await host.goto(`${ORIGIN}/app?tgWebAppStartParam=games`);
    const lobby = await env.request(1, '/lobby');
    assert(lobby.games.find(g => g.id === 'holdem').available);
    assert(await host.locator('[data-gc-quick="holdem"]').isEnabled());
    await host.locator('[data-gc-tiers="holdem"]').click();
    await host.locator('.gc-tier').first().waitFor();
    assert.equal(await host.locator('.gc-tier').count(), 6);
    assert.match(await host.locator('[data-gc-tier="supreme"]').innerText(), /不设上限/);
    await host.locator('[data-gc-tier="normal"] [data-gc-enter="create"]').click();
    await host.locator('[data-gc-amount]').fill('199');
    assert(await host.locator('[data-gc-buy-confirm]').isDisabled());
    await host.locator('[data-gc-amount]').fill('1001');
    assert(await host.locator('[data-gc-buy-confirm]').isDisabled());
    await host.locator('[data-gc-amount]').fill('400');
    await shots(host, '07-holdem-buy-in');
    await host.locator('[data-gc-buy-confirm]').click();
    await host.locator('.gc-holdem').waitFor();
    const id = await host.locator('.gc-table').getAttribute('data-table-id');
    let t = await tableFor(env, id);
    assert.equal(t.me.wallet.frozen, 400);
    assert.equal(t.me.wallet.points, initialWallet.points - 400);
    assert.equal(await host.locator('[data-gc-ready]').count(), 0);
    assert(await host.locator('[data-gc-start]').isDisabled());

    await guest.goto(`${ORIGIN}/app?tgWebAppStartParam=gc_${id}`);
    await guest.locator('[data-gc-seat]').waitFor();
    assert.equal((await tableFor(env, id, 2)).me.seat, null);
    assert.equal(countRequests(env, '/seat', 2), 0);
    await guest.locator('[data-gc-seat]').click();
    await guest.locator('[data-gc-amount]').fill('600');
    await guest.locator('[data-gc-buy-confirm]').click();
    await guest.locator('[data-gc-seat]').waitFor({ state: 'detached' });
    assert.equal((await env.request(2, '/wallet')).frozen, 600);
    await quick.goto(`${ORIGIN}/app?tgWebAppStartParam=games`);
    await quick.locator('[data-gc-quick="holdem"]').click();
    await quick.locator('[data-gc-amount]').fill('800');
    await quick.locator('[data-gc-buy-confirm]').click();
    await quick.locator('.gc-holdem').waitFor();
    assert.equal(await quick.locator('.gc-table').getAttribute('data-table-id'), id);
    assert.equal(env.requests.filter(r => r.user === 3 && r.path.endsWith('/quick-start')).at(-1).body.buy_in, 800);
    await syncTable(host, await tableFor(env, id));
    await host.locator('[data-gc-start]').click();
    await host.waitForFunction(() => document.querySelector('.gc-table')?.dataset.phase === 'playing');
    t = await tableFor(env, id);
    assert.equal(t.button, 0);
    assert.equal(t.hand.engine.seats.filter(s => Object.hasOwn(s, 'hole')).length, 1);
    assert.equal(await host.locator('.gc-hand .gc-card:not(.gc-card-back)').count(), 2);
    assert.equal(await host.locator('.gc-seat .gc-card:not(.gc-card-back)').count(), 0);
    assert(await host.locator('[data-gc-topup]').isDisabled());
    assert.equal(await host.locator('[data-gc-verify]').count(), 0);

    // Three more members join mid-hand; they do not alter the existing action deadline.
    const deadline = t.turn_deadline;
    for (let user = 4; user <= 6; user++) {
      await env.request(user, `/tables/${id}/seat`, 'POST', { buy_in: 500 });
    }
    t = await tableFor(env, id);
    assert.equal(t.turn_deadline, deadline);
    assert.equal(t.hand.engine.seats.length, 3);
    t = await holdemAction(env, pages, id, 'raise', 50);
    assert.equal(t.me.wallet.frozen, 400, 'bets do not deduct wallet a second time');
    assert.equal(t.seats[0].stack, 350);
    while (t.hand.engine.street === 'preflop') t = await holdemAction(env, pages, id);
    t = await holdemAction(env, pages, id, 'bet', 'half');
    t = await finishHoldem(env, pages, id);
    const firstNet = t.last_hand.result.players[t.me.player].net;
    assert.equal(t.phase, 'between');
    assert.equal(t.countdown_at - t.server_time, 4);
    await syncTable(host, t);
    const countdown = Number(await host.locator('[data-gc-countdown]').first().innerText());
    assert(countdown > 0 && countdown <= 4, 'countdown uses server_time, not the skewed device clock');
    assert.equal(await host.locator('[data-gc-start]').count(), 0);
    await matrix(host, null);
    await host.locator('[data-gc-history]').first().click();
    assert.equal(await host.locator('.gc-best-five .gc-card').count(), 5);
    await closeSheet(host);
    await env.call({ kind: 'advance', table: id, seconds: 2 });
    await syncTable(host, await tableFor(env, id));
    await host.waitForFunction(() => {
      const remaining = Number(document.querySelector('[data-gc-countdown]')?.textContent);
      return remaining > 0 && remaining <= 2;
    }); // The refresh can have the same revision; wait for its corrected server_time too.

    await env.call({ kind: 'advance', table: id, deadline: 'countdown' });
    t = await tableFor(env, id);
    assert.equal(t.hand_no, 2);
    assert.equal(t.hand.engine.seats.length, 6);
    assert.equal(t.button, 1);
    while (t.hand.engine.street === 'preflop' || t.hand.current_player !== t.me.player) {
      t = await holdemAction(env, pages, id);
    }
    await syncTable(host, t);
    assert.equal(await host.locator('.gc-board .gc-card').count(), 3);
    assert.equal(await host.locator('.gc-seat').count(), 5);
    assert.equal(await host.locator('.gc-croupier').getAttribute('aria-hidden'), 'true');
    assert.equal(await host.locator('.gc-croupier button,.gc-croupier [data-gc-player]').count(), 0);
    assert.match(await host.locator('.gc-croupier img').getAttribute('src'), /dealer-normal\.webp$/);
    await matrix(host, '04-holdem-table');
    // Art failure retains the decorative silhouette without disturbing the actual table.
    await host.route('**/game-assets/dealer-normal.webp', route => route.abort());
    await host.reload();
    await host.waitForFunction(() => {
      const d = document.querySelector('.gc-croupier');
      return d && !d.querySelector('img') && !d.classList.contains('has-art');
    });
    await shots(host, null);
    await host.unroute('**/game-assets/dealer-normal.webp');
    await host.reload();
    await host.locator('.gc-croupier.has-art').waitFor();

    await env.call({ kind: 'advance', table: id, deadline: 'turn' });
    await host.locator('[data-gc-takeover-sheet]').waitFor();
    assert.match(await host.locator('.gc-timeout').innerText(), /超时/);
    await closeSheet(host);
    await shots(host, '12-holdem-timeout');
    await matrix(host, null); // The persistent timeout banner must also fit six occupied seats.
    await host.locator('[data-gc-takeover]').click();
    await host.waitForFunction(() => document.querySelector('[data-gc-auto]')?.getAttribute('aria-checked') === 'false');
    await host.locator('[data-gc-auto]').click();
    await host.waitForFunction(() => document.querySelector('[data-gc-auto]')?.getAttribute('aria-checked') === 'true');
    assert((await tableFor(env, id)).seats[0].auto_since != null);
    await host.locator('[data-gc-auto]').click();
    await host.waitForFunction(() => document.querySelector('[data-gc-auto]')?.getAttribute('aria-checked') === 'false');
    t = await finishHoldem(env, pages, id);
    const secondNet = t.last_hand.result.players[t.me.player].net;
    await syncTable(host, t);
    await host.locator('[data-gc-history]').first().click();
    assert.equal(await host.locator('.gc-result-row').count(), 6);
    assert.equal(await host.locator('[data-gc-hand]').count(), 2);
    await host.locator('[data-gc-hand="1"]').click();
    assert.match(await host.locator('.gc-result-head').innerText(), /第 1 手结算/);
    await closeSheet(host);
    const beforeTopup = t.me.wallet, stack = t.seats[0].stack, nextAt = t.countdown_at;
    await host.locator('[data-gc-topup]').click();
    await host.locator('[data-gc-amount]').fill('100');
    await shots(host, '10-holdem-top-up');
    env.loseNext('/top-up');
    await host.locator('[data-gc-buy-confirm]').click();
    await host.locator('.gc-sheet [data-gc-pending]').click();
    await host.locator('.gc-sheet [data-gc-pending]').waitFor({ state: 'detached' });
    t = await tableFor(env, id);
    assert.equal(t.seats[0].stack, stack + 100);
    assert.equal(t.me.wallet.points, beforeTopup.points - 100);
    assert.equal(t.me.wallet.frozen, beforeTopup.frozen + 100);
    assert.equal(t.countdown_at, nextAt, 'top-up never postpones the next hand');
    const topups = env.requests.filter(r => r.path.endsWith('/top-up'));
    assert.equal(topups.at(-1).body.request_id, topups.at(-2).body.request_id);
    await closeSheet(host);
    await host.locator('#toast').waitFor({ state: 'hidden' }); // Do not photograph the intentionally lost-response toast.
    await host.locator('[data-gc-away]').click();
    await host.waitForFunction(() => document.querySelector('[data-gc-away]')?.textContent.includes('中'));
    t = await tableFor(env, id);
    assert(t.seats[0].away_since != null);
    await shots(host, '11-holdem-away');
    const awayStack = t.seats[0].stack;
    await env.call({ kind: 'advance', table: id, deadline: 'countdown' });
    t = await tableFor(env, id);
    assert.equal(t.hand_no, 3);
    assert.equal(t.hand.engine.seats.length, 5);
    assert(!t.hand.engine.seats.some(s => s.player === t.me.player));
    assert.equal(t.seats[0].stack, awayStack, 'away member pays no blinds');

    // Real SSE reconnect fetches a new token and snapshot without replaying an action.
    await syncTable(host, t);
    const tokens = countRequests(env, '/stream-token'), actions = countRequests(env, '/actions');
    env.failStream();
    await host.locator('[data-gc-connection]').click();
    for (let i = 0; i < 100 && countRequests(env, '/stream-token') < tokens + 2; i++) await host.waitForTimeout(50);
    assert(countRequests(env, '/stream-token') >= tokens + 2);
    assert.equal(countRequests(env, '/actions'), actions);
    await host.reload();
    await host.locator('.gc-holdem').waitFor();
    assert.equal(await host.locator('.gc-table').getAttribute('data-table-id'), id);
    // A non-participant can leave immediately, even while the other five are playing.
    await host.locator('[data-gc-leave]').click();
    await host.locator('[data-gc-leave-confirm]').click();
    await host.locator('.gc-lobby').waitFor();
    const cashout = await env.request(1, '/wallet');
    assert.equal(cashout.frozen, 0);
    assert.equal(cashout.points, initialWallet.points + firstNet + secondNet);
    assert.equal(cashout.points, t.me.wallet.points + awayStack);
    assert(env.requests.filter(r => r.path.startsWith(ROOT) && !r.path.endsWith('/events')).every(r => r.init));
    assert.equal(env.errors.length, 0, env.errors.join('\n'));
  } finally { await env.close(); }
});

test('real Holdem multiway all-ins: side pots, uncalled return, deferred cash-out, showdown and shuffle verification', { timeout: 60000 }, async () => {
  const env = await environment();
  try {
    await env.call({ kind: 'deck', front: [11,10,12,24,23,25,51,0,14,28,50,5,49,19] });
    let t = await createHoldem(env, [200, 400, 600]);
    const id = t.id, pages = new Map();
    await env.request(1, `/tables/${id}/start`, 'POST', {});
    const host = await holdemPage(env, pages, id, 1);
    t = await holdemAction(env, pages, id, 'call');
    t = await holdemAction(env, pages, id, 'all_in');
    const departing = await holdemPage(env, pages, id, 2);
    const departingBefore = await tableFor(env, id, 2);
    await departing.locator('[data-gc-leave]').click();
    await departing.locator('[data-gc-leave-confirm]').click();
    await departing.waitForFunction(() => document.querySelector('.gc-wait-bar')?.textContent.includes('结算'));
    assert(await departing.locator('[data-gc-auto]').isDisabled());
    assert(await departing.locator('[data-gc-away]').isDisabled());
    t = await holdemAction(env, pages, id, 'all_in');
    await syncTable(host, t);
    assert.match(await host.locator('.gc-pots').innerText(), /边池/);
    await shots(host, '09-holdem-side-pots');
    t = await holdemAction(env, pages, id, 'all_in');
    assert.equal(t.phase, 'between');
    const result = t.last_hand.result;
    assert.deepEqual(result.pots.map(p => p.amount), [600, 400]);
    assert.deepEqual(Object.values(result.uncalled), [200]);
    assert.equal(Object.values(result.players).reduce((n, p) => n + p.net, 0), 0);
    const departed = departingBefore.me.player;
    assert(result.pots[1].winners.includes(departed), 'all-in leaver keeps side-pot eligibility');
    const wallet = await env.request(2, '/wallet');
    assert.equal(wallet.frozen, 0);
    assert.equal(wallet.points, departingBefore.me.wallet.points + result.players[departed].stack);
    assert.equal((await tableFor(env, id, 2)).me.seat, null);
    await syncTable(host, t);
    await host.locator('[data-gc-history]').first().click();
    assert.equal(await host.locator('.gc-best-five .gc-card').count(), 5);
    assert.match(await host.locator('.gc-payouts').innerText(), /主池 600[\s\S]*边池 1 400[\s\S]*未跟注退回/);
    const otherShown = t.last_hand.engine.seats.find(p => p.player !== t.me.player && p.hole);
    await host.locator(`[data-gc-focus="${otherShown.player}"]`).click();
    assert.equal(await host.locator('.gc-best-five .gc-card').count(), 5);
    await shots(host, '08-holdem-results');
    assert.equal(env.errors.length, 0, env.errors.join('\n'));
  } finally { await env.close(); }
});

test('real Holdem short all-in does not reopen raise rights or synthesize all-in buttons', { timeout: 60000 }, async () => {
  const env = await environment();
  try {
    const t = await createHoldem(env, [400, 250, 400]);
    const id = t.id, pages = new Map();
    await env.request(1, `/tables/${id}/start`, 'POST', {});
    await holdemAction(env, pages, id, 'raise', 200);
    await holdemAction(env, pages, id, 'all_in');
    const state = await holdemAction(env, pages, id, 'call');
    assert.equal(state.hand.current_player, state.me.player);
    assert.equal(state.hand.legal_actions.call, 50);
    assert.equal(state.hand.legal_actions.min_raise_to, null);
    assert.equal(state.hand.legal_actions.all_in, 0);
    const host = pages.get(1);
    await syncTable(host, state);
    assert.equal(await host.locator('[data-gc-action="raise"],[data-gc-action="bet"],[data-gc-action="all_in"],[data-gc-action="min_raise_to"]').count(), 0);
    await holdemAction(env, pages, id, 'call');
    const folder = (await tableFor(env, id)).hand.current_player;
    await holdemAction(env, pages, id, 'fold');
    const settled = await finishHoldem(env, pages, id);
    assert.equal(settled.last_hand.engine.seats.find(p => p.player === folder).hole, undefined);
    assert.equal(settled.last_hand.result.players[folder].shown, false);
    assert.equal(env.errors.length, 0, env.errors.join('\n'));
  } finally { await env.close(); }
});

test('real Holdem practice: five robots, beans-only bust/pause/top-up, away/resume and deferred leaving', { timeout: 60000 }, async () => {
  const env = await environment();
  try {
    await env.call({ kind: 'deck', front: [12,11,10,9,8,0,25,24,23,22,21,14,51,3,18,33,50,6,49,28] });
    const page = await env.pageFor(), pages = new Map([[1, page]]);
    await env.call({ kind: 'balance', user: 1, amount: 20 });
    const initial = await env.request(1, '/wallet');
    await page.goto(`${ORIGIN}/app?tgWebAppStartParam=games`);
    await page.locator('[data-gc-tiers="holdem"]').click();
    await page.locator('.gc-tier').first().waitFor();
    assert(await page.locator('[data-gc-tier="novice"] [data-gc-enter="quick"]').isDisabled());
    await page.locator('[data-gc-tier="practice"] [data-gc-enter="quick"]').click();
    assert.equal(await page.locator('[data-gc-amount]').inputValue(), '2000');
    await page.locator('[data-gc-buy-confirm]').click();
    await page.locator('.gc-holdem').waitFor();
    const id = await page.locator('.gc-table').getAttribute('data-table-id');
    let t = await tableFor(env, id);
    assert.equal(t.phase, 'playing', 'six seats start the first practice hand automatically');
    assert.equal(t.seats.filter(s => s.bot).length, 5);
    assert.equal(await page.locator('.gc-bot-label').count(), 5);
    assert.equal(t.me.wallet.points, initial.points);
    assert.equal(t.me.wallet.beans, initial.beans - 2000);
    const advanceBots = async () => {
      for (let i = 0; i < 100; i++) {
        t = await tableFor(env, id);
        if (!t.hand || t.hand.current_player === t.me.player) return;
        assert(t.auto_at != null, 'robot has a durable scheduled action');
        await env.call({ kind: 'advance', table: id, deadline: 'auto' });
      }
      assert.fail('robots failed to yield/finish');
    };
    await advanceBots();
    await holdemAction(env, pages, id, 'all_in');
    await advanceBots();
    t = await tableFor(env, id);
    assert.equal(t.seats[0].stack, 0);
    assert.equal(t.phase, 'waiting', 'practice pauses rather than running bot-only hands');
    assert.equal(t.countdown_at, null);
    await syncTable(page, t);
    assert.match(await page.locator('.gc-wait-bar').innerText(), /筹码已用完/);
    await matrix(page, null);
    await env.call({ kind: 'advance', table: id, seconds: 60 });
    assert.equal((await tableFor(env, id)).hand_no, 1);
    await page.locator('[data-gc-topup]').click();
    await page.locator('[data-gc-amount]').fill('2000');
    await page.locator('[data-gc-buy-confirm]').click();
    await page.waitForFunction(() => document.querySelector('.gc-table')?.dataset.phase === 'between');
    await page.locator('[data-gc-away]').click();
    await page.waitForFunction(() => document.querySelector('.gc-table')?.dataset.phase === 'waiting');
    t = await tableFor(env, id);
    assert(t.seats[0].away_since != null);
    assert.equal(t.countdown_at, null);
    await env.call({ kind: 'advance', table: id, seconds: 60 });
    assert.equal((await tableFor(env, id)).hand_no, 1);
    await page.locator('[data-gc-away]').click();
    await page.waitForFunction(() => document.querySelector('.gc-table')?.dataset.phase === 'between');
    await env.call({ kind: 'advance', table: id, deadline: 'countdown' });
    await advanceBots();
    t = await tableFor(env, id);
    assert.equal(t.hand_no, 2);
    await syncTable(page, t);
    const beforeLeave = t.me.wallet, stack = t.seats[0].stack;
    await page.locator('[data-gc-leave]').click();
    await page.locator('[data-gc-leave-confirm]').click();
    await page.waitForFunction(() => document.querySelector('.gc-wait-bar')?.textContent.includes('结算'));
    assert.equal(await page.locator('[data-gc-action]').count(), 0);
    await env.call({ kind: 'advance', table: id, deadline: 'auto' });
    await advanceBots();
    t = await tableFor(env, id);
    assert.equal(t.phase, 'closed');
    const wallet = await env.request(1, '/wallet');
    assert.equal(wallet.points, initial.points);
    assert.equal(wallet.frozen, 0);
    assert.equal(wallet.beans, beforeLeave.beans + stack);
    assert.equal(env.errors.length, 0, env.errors.join('\n'));
  } finally { await env.close(); }
});

test('table effects: action bubbles and an on-felt celebration precede exactly one result sheet', { timeout: 60000 }, async () => {
  const env = await environment();
  try {
    const host = await env.pageFor(1, 'dark', 400, 860, false, true);
    let t = (await env.request(1, '/tables', 'POST', { game: 'zjh', tier: 'novice' })).table;
    const id = t.id;
    const startHand = async () => {
      await env.request(2, `/tables/${id}/ready`, 'POST', { ready: true });
      await env.request(1, `/tables/${id}/start`, 'POST', {});
    };
    const act = async (user, action) => {
      const view = await tableFor(env, id, user);
      await env.request(user, `/tables/${id}/actions`, 'POST', { hand_no: view.hand_no, turn: view.hand.turn, action });
      await syncTable(host, await tableFor(env, id));
    };
    await env.request(2, `/tables/${id}/seat`, 'POST', {});
    await startHand();
    await host.goto(ORIGIN + '/app?tgWebAppStartParam=gc_' + id);
    await host.locator('.gc-table[data-phase="playing"]').waitFor();
    const decoded = await host.evaluate(async () => {
      const ctx = new AudioContext();
      const names = ['deal', 'flip', 'chip', 'raise', 'allin', 'fold', 'check', 'turn', 'tick', 'win', 'lose', 'tap', 'music'];
      return Promise.all(names.map(async name => {
        const data = await (await fetch(`/app/assets/game-assets/audio/${name}.mp3`)).arrayBuffer();
        return (await ctx.decodeAudioData(data)).duration;
      }));
    });
    assert(decoded.every(seconds => seconds > .05), `every sound decodes: ${decoded}`);
    // Looking is out of turn and silent on the server; everyone else still sees it happen.
    await act(2, 'look');
    await host.locator('.gc-fx-bubble', { hasText: '看牌' }).waitFor();
    t = await tableFor(env, id);
    if (t.hand.current_player === t.me.player) {
      await act(1, 'call');
      await host.locator('.gc-fx-bubble', { hasText: '跟注' }).waitFor();
    }
    await act(2, 'fold');
    await host.locator('.gc-fx-bubble', { hasText: '弃牌' }).waitFor();
    await host.locator('.gc-celebrate').waitFor();
    assert.match(await host.locator('.gc-celebrate').innerText(), /可乐 赢了/);
    assert.equal(await host.locator('.gc-sheet').count(), 0, 'the result sheet waits for the celebration');
    await capture(host, '13-zjh-celebrate.png');
    await host.locator('.gc-result-head').waitFor({ timeout: 6000 });
    await closeSheet(host);
    await startHand();
    await syncTable(host, await tableFor(env, id));
    await host.locator('.gc-table[data-phase="playing"]').waitFor();
    t = await tableFor(env, id);
    await act(t.hand.current_player === t.me.player ? 1 : 2, 'call');
    await host.waitForTimeout(500);
    assert.equal(await host.locator('.gc-sheet').count(), 0, 'hand 1 result must not reopen during hand 2');
    assert.equal(env.errors.length, 0, env.errors.join('\n'));
  } finally { await env.close(); }
});

test('hold\'em celebrates in its own centre without colliding with six-seat positions', { timeout: 60000 }, async () => {
  const env = await environment();
  try {
    const host = await env.pageFor(1, 'dark', 400, 860, false, true);
    let h = (await env.request(1, '/tables', 'POST', { game: 'holdem', tier: 'novice', buy_in: 100 })).table;
    await env.request(2, `/tables/${h.id}/seat`, 'POST', { buy_in: 100 });
    await env.request(1, `/tables/${h.id}/start`, 'POST', {});
    await host.goto(ORIGIN + '/app?tgWebAppStartParam=gc_' + h.id);
    await host.locator('.gc-table[data-phase="playing"]').waitFor();
    h = await tableFor(env, h.id);
    const folder = h.hand.current_player === h.me.player ? 1 : 2;
    const view = await tableFor(env, h.id, folder);
    await env.request(folder, `/tables/${h.id}/actions`, 'POST', { hand_no: view.hand_no, turn: view.hand.turn, action: 'fold' });
    await syncTable(host, await tableFor(env, h.id));
    await host.locator('.gc-celebrate').waitFor();
    await capture(host, '14-holdem-celebrate.png');
    assert.equal(env.errors.length, 0, env.errors.join('\n'));
  } finally { await env.close(); }
});

test('connection warning waits for repeated failures; token renewal is quiet and recovery clears it', { timeout: 60000 }, async () => {
  const env = await environment();
  try {
    const { table } = await env.request(1, '/tables', 'POST', { game: 'zjh', tier: 'novice' });
    const page = await env.pageFor(1, 'dark', 400, 860, true);
    // Only the event delivery and token lifetime are controlled. Page, HTTP, DB and snapshots are real.
    let tokenFailures = 0;
    env.simulate(({ path: p }) => {
      if (!p.endsWith('/stream-token')) return null;
      if (tokenFailures > 0) {
        tokenFailures--;
        return { status: 503, body: { detail: 'temporary stream-token outage' } };
      }
      return { body: {
        token: 'timing_test', expires_at: table.server_time + 120,
        url: `${ROOT}/tables/${table.id}/events?token=timing_test`,
      } };
    });
    await page.clock.install({ time: new Date('2026-01-01T00:00:00Z') });
    await page.clock.pauseAt(new Date('2026-01-02T00:00:00Z'));
    await page.goto(`${ORIGIN}/app?tgWebAppStartParam=gc_${table.id}`);
    const status = page.locator('[data-gc-connection]');
    const waitStreams = async count => {
      for (let i = 0; i < 100; i++) {
        if (await page.evaluate(n => window.__streams.length >= n, count)) return;
        await page.waitForTimeout(20);
      }
      assert.fail(`expected ${count} streams`);
    };
    const snapshot = () => page.evaluate(table => {
      window.__streams.at(-1).dispatchEvent(new MessageEvent('snapshot', { data: JSON.stringify({ table }) }));
    }, table);
    const failStream = () => page.evaluate(() => window.__streams.at(-1).onerror(new Event('error')));
    await waitStreams(1);
    await snapshot();
    assert.equal(await status.getAttribute('data-state'), 'live');
    await page.evaluate(() => {
      window.__connectionStates = [];
      new MutationObserver(() => {
        window.__connectionStates.push(document.querySelector('[data-gc-connection]')?.dataset.state);
      }).observe(document.querySelector('#screen'), { subtree: true, attributes: true, childList: true });
    });

    // Advance the actual renewal timer, not a click on the reconnect button.
    await page.clock.runFor(118001);
    await waitStreams(2);
    assert(await page.evaluate(() => window.__streams[0].testClosed));
    assert.equal(await status.getAttribute('data-state'), 'live');
    assert(await page.evaluate(() => window.__connectionStates.every(s => s === 'live')));
    // Late errors from the intentionally closed generation must not affect the replacement.
    await page.evaluate(() => window.__streams[0].onerror(new Event('error')));
    await snapshot();

    // A dropped stream and one failed retry stay neutral; two failed retries show the warning.
    await failStream();
    assert.equal(await status.getAttribute('data-state'), 'connecting');
    await page.clock.runFor(800);
    await waitStreams(3);
    await failStream();
    assert.equal(await status.getAttribute('data-state'), 'connecting');
    assert(!(await page.evaluate(() => window.__connectionStates.includes('retry'))));
    await page.clock.runFor(1600);
    await waitStreams(4);
    await failStream();
    assert.equal(await status.innerText(), '连接中断 · 自动重连');
    await page.clock.runFor(3200);
    await waitStreams(5);
    await snapshot();
    assert.equal(await status.getAttribute('data-state'), 'live');

    // Recovery resets the streak. Token HTTP failures count too, not just EventSource errors.
    tokenFailures = 2;
    await failStream();
    assert.equal(await status.getAttribute('data-state'), 'connecting');
    const tokensBefore = countRequests(env, '/stream-token');
    const waitTokenFailure = async remaining => {
      for (let i = 0; i < 100 && tokenFailures !== remaining; i++) await page.waitForTimeout(20);
      assert.equal(tokenFailures, remaining);
      // Let the fulfilled HTTP error finish the frontend catch path before advancing time.
      await page.waitForTimeout(50);
    };
    await page.clock.runFor(800);
    await waitTokenFailure(1);
    assert.equal(await status.getAttribute('data-state'), 'connecting');
    await page.clock.runFor(1600);
    await waitTokenFailure(0);
    assert.equal(await status.getAttribute('data-state'), 'retry');
    await page.clock.runFor(3200);
    await waitStreams(6);
    await snapshot();
    assert.equal(countRequests(env, '/stream-token'), tokensBefore + 3);
    assert.equal(await status.getAttribute('data-state'), 'live');
    assert.equal(env.errors.length, 0, env.errors.join('\n'));
  } finally { await env.close(); }
});

// Board games: real rules engine, clocks and settlement behind the shipped board UI.
const boardView = (env, id, user) => tableFor(env, id, user);
// Phones inside Telegram leave about 390x700; the board must still take nearly the whole width.
async function boardFill(page, label) {
  for (const [width, height, least] of [[390, 700, .94], [360, 640, .9]]) {
    await page.setViewportSize({ width, height });
    const fill = await page.evaluate(() => document.querySelector('[data-gc-board]').getBoundingClientRect().width / innerWidth);
    assert(fill >= least, `${label} board fills ${width}x${height}: ${fill.toFixed(3)}`);
    await capture(page, `${label}-${width}x${height}.png`);
  }
  await page.setViewportSize({ width: 400, height: 860 });
}
async function boardMove(env, id, user, move) {
  const v = await boardView(env, id, user);
  return (await env.request(user, `/tables/${id}/actions`, 'POST', { hand_no: v.hand_no, turn: v.hand.turn, action: 'move', move })).table;
}
const pieceCount = page => page.locator('.gc-xq-piece').count();
const stoneCount = page => page.locator('.gc-stone:not(.ghost)').count();

test('real xiangqi: colours and flipped board, tap-to-move, capture with check, draw offer, resign and rematch', { timeout: 90000 }, async () => {
  const env = await environment();
  try {
    let t = (await env.request(1, '/tables', 'POST', { game: 'xiangqi', tier: 'normal' })).table;
    const id = t.id;
    await env.request(2, `/tables/${id}/seat`, 'POST', {});
    const host = await env.pageFor(1, 'dark', 400, 860, false, true), guest = await env.pageFor(2, 'dark', 400, 860, false, true);
    for (const page of [host, guest]) {
      await page.goto(ORIGIN + '/app?tgWebAppStartParam=gc_' + id);
      await page.locator('.gc-board-table[data-phase="waiting"]').waitFor();
    }
    assert.equal(await host.locator('[data-gc-board-act]').count(), 0, 'no board actions before the game starts');
    await capture(host, '20-xiangqi-waiting.png');
    await env.request(2, `/tables/${id}/ready`, 'POST', { ready: true });
    await env.request(1, `/tables/${id}/start`, 'POST', {});
    t = await boardView(env, id, 1);
    const red = t.hand.players.find(p => p.color === 'red').player === t.me.player ? 1 : 2;
    const black = 3 - red, pages = { 1: host, 2: guest };
    for (const page of [host, guest]) await syncTable(page, await boardView(env, id, page === host ? 1 : 2));
    // Each side sees its own colour at the bottom: red's king low on red's board, high on black's.
    const kingY = async (page, sq) => (await page.locator(`[data-gc-sq="${sq}"]`).boundingBox()).y;
    assert(await kingY(pages[red], 'e0') > await kingY(pages[red], 'e9'), 'red plays from the bottom');
    assert(await kingY(pages[black], 'e0') < await kingY(pages[black], 'e9'), 'black sees a flipped board');
    assert.match(await pages[red].locator('.gc-duel-bar.bottom').innerText(), /红方/);
    assert.match(await pages[black].locator('.gc-duel-bar.bottom').innerText(), /黑方/);
    // Only the side to move gets legal targets; selecting shows them, tapping one moves.
    await pages[black].locator('[data-gc-sq="h7"]').click();
    assert.equal(await pages[black].locator('.gc-xq-dot').count(), 0, 'waiting side cannot select');
    await pages[red].locator('[data-gc-sq="h2"]').click();
    await pages[red].locator('.gc-xq-piece.sel[data-gc-sq="h2"]').waitFor();
    assert(await pages[red].locator('.gc-xq-dot').count() >= 5, 'legal destinations are shown');
    await capture(pages[red], '21-xiangqi-selected.png');
    await pages[red].locator('.gc-xq-dot[data-gc-sq="e2"]').click();
    await pages[red].locator('.gc-xq-piece[data-gc-sq="e2"]').waitFor();
    assert.equal(await pages[red].locator('.gc-xq-last').count(), 2, 'last move is marked');
    await syncTable(pages[black], await boardView(env, id, black));
    await pages[black].locator('[data-gc-sq="h7"]').click();
    await pages[black].locator('.gc-xq-dot[data-gc-sq="e7"]').click();
    await pages[black].locator('.gc-xq-piece[data-gc-sq="e7"]').waitFor();
    // Red's cannon jumps its pawn, takes the centre pawn and checks through black's own cannon.
    await syncTable(pages[red], await boardView(env, id, red));
    await pages[red].locator('[data-gc-sq="e2"]').click();
    await pages[red].locator('.gc-xq-piece.target[data-gc-sq="e6"]').click();
    await pages[red].waitForFunction(() => document.querySelectorAll('.gc-xq-piece').length === 31);
    await syncTable(pages[black], await boardView(env, id, black));
    await pages[black].locator('.gc-xq-piece.check[data-gc-sq="e9"]').waitFor();
    await pages[black].locator(`.gc-fx-bubble`, { hasText: '将军' }).waitFor();
    assert.match(await pages[black].locator('.gc-duel-bar.bottom').innerText(), /轮到你走 · 被将军/);
    assert.equal(await pieceCount(pages[black]), 31);
    await capture(pages[black], '22-xiangqi-check-flipped.png');
    await boardFill(pages[black], '22-xiangqi-check');
    // Draw offer: the other side may answer out of turn; rejecting keeps the game going.
    await pages[black].locator('[data-gc-board-act="offer_draw"]').click();
    await pages[black].locator('.gc-wait-reason', { hasText: '已提出和棋' }).waitFor();
    await syncTable(pages[red], await boardView(env, id, red));
    await pages[red].locator('.gc-draw-offer').waitFor();
    await capture(pages[red], '23-xiangqi-draw-offer.png');
    await boardFill(pages[red], '23-xiangqi-draw-offer');
    await pages[red].locator('[data-gc-board-act="reject_draw"]').click();
    await pages[red].locator('.gc-draw-offer').waitFor({ state: 'detached' });
    await syncTable(pages[black], await boardView(env, id, black));
    assert.equal(await pages[black].locator('.gc-wait-reason').count(), 0, 'the rejected offer is gone for the offerer too');
    // Resign through the confirmation sheet; both boards keep the final position under a ribbon.
    const total = w => w.points + w.frozen, before = total((await boardView(env, id, red)).me.wallet);
    await pages[black].locator('[data-gc-board-act="resign"]').click();
    await pages[black].locator('.gc-sheet [data-gc-resign]').click();
    await pages[black].locator('.gc-board-result.loss').waitFor();
    assert.match(await pages[black].locator('.gc-board-result').innerText(), /你输了[\s\S]*你认输了 · [−-]50 积分/);
    await syncTable(pages[red], await boardView(env, id, red));
    await pages[red].locator('.gc-board-result.win').waitFor();
    assert.match(await pages[red].locator('.gc-board-result').innerText(), /你赢了[\s\S]*对方认输 · \+50 积分/);
    assert.equal(total((await boardView(env, id, red)).me.wallet), before + 50, 'winner takes both stakes, no rake');
    assert.equal(await pieceCount(pages[red]), 31, 'final position stays on the board');
    assert.equal(await pages[red].locator('.gc-board-frame .gc-board-result').count(), 0, 'the result never covers the board');
    assert.match(await pages[black].locator('.gc-board-dock').innerText(), /点「准备」后，房主就能开局|等待对手点准备/);
    await shots(pages[red], '24-xiangqi-result');
    await pages[red].locator('.gc-board-result').click();
    await pages[red].locator('.gc-sheet .gc-result-head', { hasText: '你赢了' }).waitFor();
    assert.match(await pages[red].locator('.gc-sheet').innerText(), /共 3 手/);
    await closeSheet(pages[red]);
    // Rematch: each side readies again and the colours swap.
    assert.match(await pages[red].locator('[data-gc-ready]').innerText(), /再来一局/);
    await pages[red].locator('[data-gc-ready]').click();
    await env.request(black, `/tables/${id}/ready`, 'POST', { ready: true });
    if (red !== 1) await pages[red].locator('[data-gc-ready]').waitFor();
    const starter = (await boardView(env, id, 1)).host === (await boardView(env, id, 1)).me.player ? 1 : 2;
    await env.request(starter, `/tables/${id}/start`, 'POST', {});
    t = await boardView(env, id, red);
    assert.equal(t.hand.players.find(p => p.player === t.me.player).color, 'black', 'colours swap each game');
    await syncTable(pages[red], t);
    assert(await kingY(pages[red], 'e0') < await kingY(pages[red], 'e9'), 'the board flips with the new colour');
    await matrix(pages[red]);
    assert.equal(env.errors.length, 0, env.errors.join('\n'));
  } finally { await env.close(); }
});

test('real gomoku: two humans, preview-then-confirm placement, five in a row and clocks', { timeout: 90000 }, async () => {
  const env = await environment();
  try {
    let t = (await env.request(1, '/tables', 'POST', { game: 'gomoku', tier: 'normal' })).table;
    const id = t.id;
    await env.request(2, `/tables/${id}/seat`, 'POST', {});
    await env.request(2, `/tables/${id}/ready`, 'POST', { ready: true });
    await env.request(1, `/tables/${id}/start`, 'POST', {});
    t = await boardView(env, id, 1);
    const blackUser = t.hand.players.find(p => p.color === 'black').player === t.me.player ? 1 : 2, whiteUser = 3 - blackUser;
    const page = await env.pageFor(blackUser, 'dark', 400, 860, false, true);
    await page.goto(ORIGIN + '/app?tgWebAppStartParam=gc_' + id);
    await page.locator('.gc-board-table[data-phase="playing"]').waitFor();
    assert.match(await page.locator('.gc-duel-bar.bottom').innerText(), /执黑/);
    assert.match(await page.locator('.gc-duel-bar.bottom .gc-clock').innerText(), /^\d+:\d\d/, 'clock shows minutes:seconds');
    const tap = async (x, y) => {
      const box = await page.locator('[data-gc-board]').boundingBox();
      await page.mouse.click(box.x + box.width * (50 + x * 100) / 1500, box.y + box.height * (50 + y * 100) / 1500);
    };
    // Black builds four on row 7 via the UI and API; white answers elsewhere.
    const whites = [[0, 0], [0, 2], [0, 4], [0, 6]];
    for (let i = 0; i < 4; i++) {
      await tap(5 + i, 7);
      await page.locator('.gc-stone.ghost').waitFor();
      assert.equal(await page.locator('[data-gc-board-act="place"]').isDisabled(), false);
      if (i === 0) await capture(page, '25-gomoku-preview.png');
      if (i % 2) await tap(5 + i, 7); else await page.locator('[data-gc-board-act="place"]').click();
      await page.waitForFunction(n => document.querySelectorAll('.gc-stone:not(.ghost)').length === n, i * 2 + 1);
      await boardMove(env, id, whiteUser, whites[i]);
      await syncTable(page, await boardView(env, id, blackUser));
    }
    assert.equal(await stoneCount(page), 8);
    assert.equal(await page.locator('.gc-stone.last').count(), 1, 'the last stone is marked');
    await boardFill(page, '26-gomoku-playing');
    // Occupied points are ignored; the fifth stone wins and the line is highlighted.
    await tap(5, 7);
    assert.equal(await page.locator('.gc-stone.ghost').count(), 0, 'occupied points cannot be previewed');
    await tap(9, 7);
    await page.locator('[data-gc-board-act="place"]').click();
    await page.locator('.gc-board-result.win').waitFor();
    assert.equal(await page.locator('.gc-stone-win').count(), 5, 'winning five is highlighted');
    const covered = await page.evaluate(() => [...document.querySelectorAll('.gc-stone-win')].some(w => {
      const r = w.getBoundingClientRect(), top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return top && !top.closest('[data-gc-board]');
    }));
    assert.equal(covered, false, 'nothing covers the winning line');
    assert.match(await page.locator('.gc-board-result').innerText(), /你赢了[\s\S]*连成五子 · \+50 积分/);
    await capture(page, '26-gomoku-five.png');
    assert.equal(env.errors.length, 0, env.errors.join('\n'));
  } finally { await env.close(); }
});

test('real gomoku practice: robot replies on the server clock, undo takes back a full round, beans only', { timeout: 90000 }, async () => {
  const env = await environment();
  try {
    const page = await env.pageFor(1, 'dark', 400, 860, false, true);
    let t = (await env.request(1, '/tables', 'POST', { game: 'gomoku', tier: 'practice', robot_level: 'easy' })).table;
    const id = t.id;
    assert.equal(t.seats.length, 2, 'the robot takes the other seat');
    const opening = t.me.wallet;
    await env.request(1, `/tables/${id}/start`, 'POST', {});
    await page.goto(ORIGIN + '/app?tgWebAppStartParam=gc_' + id);
    await page.locator('.gc-board-table[data-phase="playing"]').waitFor();
    assert.match(await page.locator('.gc-board-meta').innerText(), /入门机器人/);
    const robotTurn = async () => {
      t = await env.call({ kind: 'advance', table: id, seconds: 2 });
      await syncTable(page, await boardView(env, id, 1));
    };
    t = await boardView(env, id, 1);
    if (t.hand.current_player !== t.me.player) await robotTurn();
    const tap = async (x, y) => {
      const box = await page.locator('[data-gc-board]').boundingBox();
      await page.mouse.click(box.x + box.width * (50 + x * 100) / 1500, box.y + box.height * (50 + y * 100) / 1500);
    };
    const start = await stoneCount(page);
    for (const x of [3, 11]) {
      const before = await stoneCount(page);
      await tap(x, 3);
      await tap(x, 3);
      await page.waitForFunction(n => document.querySelectorAll('.gc-stone:not(.ghost)').length === n, before + 1);
      assert.match(await page.locator('.gc-duel-bar.top').innerText(), /思考中/);
      await robotTurn();
      assert.equal(await stoneCount(page), before + 2, 'the robot answered');
    }
    await page.locator('[data-gc-board-act="undo"]').waitFor();
    await capture(page, '27-gomoku-practice.png');
    await page.locator('[data-gc-board-act="undo"]').click();
    await page.waitForFunction(n => document.querySelectorAll('.gc-stone:not(.ghost)').length === n, start + 2);
    t = await boardView(env, id, 1);
    assert.equal(t.hand.current_player, t.me.player, 'undo returns the turn to the human');
    await page.locator('[data-gc-board-act="resign"]').click();
    assert.match(await page.locator('.gc-sheet').innerText(), /本局判负。/);
    await page.locator('.gc-sheet [data-gc-resign]').click();
    await page.locator('.gc-board-result.loss').waitFor();
    assert.match(await page.locator('.gc-board-result').innerText(), /你认输了 · [−-]100 欢乐豆/);
    t = await boardView(env, id, 1);
    assert.equal(t.me.wallet.beans, opening.beans - 100, 'the loser pays one stake of beans');
    assert.deepEqual([t.me.wallet.points, t.me.wallet.frozen], [opening.points, opening.frozen], 'practice never touches points');
    assert.equal(env.errors.length, 0, env.errors.join('\n'));
  } finally { await env.close(); }
});
