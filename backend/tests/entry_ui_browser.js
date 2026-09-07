/* Executed by the Chromium regression against the actual app.js. */
(async () => {
  let checks = 0;
  const assert = (ok, label) => { if (!ok) throw new Error(label); checks++; };
  const report = document.querySelector('#report');
  const rows = [{id: 'friend-a', origin: 'https://friend-a.example.com',
    proxy_key_set: true, future_field: {keep: true}}];
  let saved = {external_entries: rows, external_entries_revision: 'r1'};
  let writes = 0;
  const leaks = [];
  const messages = [];
  const marker = 'synthetic-export-marker';
  console.log = (...args) => leaks.push(args.join(' '));
  Storage.prototype.setItem = (...args) => leaks.push(args.join(' '));
  toast = (message) => messages.push(message);
  window.confirm = () => true;
  renderPage = async () => {
    $('#ee-list').dataset.revision = saved.external_entries_revision;
    $('#ee-list').innerHTML = entryRows(saved.external_entries);
    $('#ee-out').textContent = '';
  };
  const normalApi = async (path, opts) => {
    if (opts) {
      writes++;
      saved = JSON.parse(opts.body);
      saved.external_entries_revision = 'r' + (writes + 1);
    }
    return structuredClone(saved);
  };
  api = normalApi;
  try {
    await renderPage();
    const evil = '\"><img src=x onerror="window.injected=true">\'&';
    $('#ee-list').innerHTML = entryRows([{id: evil, origin: evil}]);
    assert(!$('#ee-list img'), 'entry HTML injection');
    assert($('#ee-list button').dataset.id === evil, 'attribute escaping');
    assert($('#ee-list').textContent.includes(evil), 'text escaping');
    await renderPage();

    $('#ee-id').value = 'friend-b';
    $('#ee-origin').value = 'https://friend-b.example.com';
    await addEntry();
    assert(writes === 1 && saved.external_entries.length === 2, 'add entry');
    assert(saved.external_entries[0].future_field.keep, 'preserve unedited fields');
    await rotateEntry('friend-a');
    assert(saved.external_entries[0].rotate_proxy_key === true, 'rotate selected entry');
    assert(saved.external_entries[1].rotate_proxy_key === false, 'keep other keys');
    await removeEntry('friend-b');
    assert(saved.external_entries.length === 1, 'remove only selected entry');
    const before = writes;
    saved.external_entries_revision = 'changed-by-other-tab';
    await rotateEntry('friend-a');
    assert(writes === before && messages.at(-1).includes('刷新'), 'stale page refused');
    await renderPage();

    const pending = [];
    api = () => new Promise((resolve) => pending.push(resolve));
    const first = exportEntry('friend-a');
    const second = exportEntry('friend-b');
    pending[1]({config: marker + '-second</textarea><img src=x onerror="window.injected=true">'});
    await second;
    pending[0]({config: marker + '-first'});
    await first;
    assert($('#ee-config').value.startsWith(marker + '-second'), 'latest export wins');
    assert(!$('#ee-out img') && !window.injected, 'config rendered as plain text');

    Object.defineProperty(navigator, 'clipboard', {configurable: true, value: undefined});
    document.execCommand = () => false;
    await $('#ee-copy').onclick();
    assert($('#ee-config').selectionStart === 0 &&
      $('#ee-config').selectionEnd === $('#ee-config').value.length, 'manual clipboard selection');
    assert($('#ee-copy-hint').textContent.includes('Ctrl+C'), 'manual copy hint');
    document.execCommand = () => true;
    await $('#ee-copy').onclick();
    assert($('#ee-copy-hint').textContent.includes('已复制'), 'legacy clipboard fallback');
    let copied;
    Object.defineProperty(navigator, 'clipboard', {value: {writeText: async (text) => { copied = text; }}});
    await $('#ee-copy').onclick();
    assert(copied === $('#ee-config').value, 'secure clipboard content');

    const obsolete = exportEntry('friend-a');
    api = normalApi;
    await rotateEntry('friend-a');
    pending[2]({config: marker + '-obsolete'});
    await obsolete;
    assert(!$('#ee-out').textContent.includes(marker) && !$('#ee-config'), 'rotation discards pending export');

    let releaseRead;
    api = (path, opts) => opts ? normalApi(path, opts)
      : new Promise((resolve) => { releaseRead = () => resolve(structuredClone(saved)); });
    const beforeBusy = writes;
    const mutation = rotateEntry('friend-a');
    assert($('#ee-add').disabled, 'busy buttons disabled');
    await rotateEntry('friend-a');
    releaseRead();
    await mutation;
    assert(writes === beforeBusy + 1, 'double click cannot rotate twice');
    assert(!$('#ee-add').disabled, 'buttons restored');
    assert(!leaks.some((text) => text.includes(marker)), 'no credential storage or console logs');
    report.textContent = JSON.stringify({ok: true, checks});
  } catch (error) {
    report.textContent = JSON.stringify({ok: false, checks, error: error.message});
  }
})();
