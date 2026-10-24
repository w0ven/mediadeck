/* Consented device check for strict lotteries.
 *
 * Nothing is read when this file loads. The page calls window.__mdx.collect()
 * ONLY after the user tapped 「同意并报名」. Requires vendor/thumbmark.umd.js
 * (ThumbmarkJS, MIT, see vendor/README.md) to be loaded before that call.
 *
 * collect() -> Promise<string>: base64url (no padding) of UTF-8 JSON
 *   {"v":1,"k":"<64 hex installation id>","s":{...}}
 * `s` only holds values that stay identical for every Telegram client on one
 * device (no language, userAgent, plugins, permissions, viewport or
 * Telegram.WebApp data). Rendering/audio/font results are SHA-256 digests.
 * No network request, no logging; a failed item is simply absent.
 */
'use strict';
(() => {
  const KEY = 'md.ux.k';
  const LEGACY = 'mediadeck.lottery.browser.v1';
  const HEX = /^[0-9a-f]{64}$/;
  // Library components that are never run: client/locale dependent or not needed.
  const SKIP = ['math', 'permissions', 'plugins', 'webrtc', 'speech'];
  // Same minimum as the server; a smaller result is not cached so a retry recollects.
  const MINIMUM = 5;
  let pending = null;

  const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  const usable = (value) => !!value && typeof value === 'object' && value.timeout !== 'true';
  const text = (value, max) => (typeof value === 'string' && value.trim() && value.trim().length <= max
    ? value.trim() : null);

  function installation() {
    try {
      let value = localStorage.getItem(KEY);
      if (HEX.test(value || '')) return value;
      const legacy = localStorage.getItem(LEGACY);
      value = HEX.test(legacy || '') ? legacy : hex(crypto.getRandomValues(new Uint8Array(32)));
      localStorage.setItem(KEY, value);
      if (localStorage.getItem(KEY) !== value) return null;
      if (value === legacy) localStorage.removeItem(LEGACY);  // migrated, then removed
      return value;
    } catch (_) {
      return null;
    }
  }

  async function sha256(value) {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
    return hex(new Uint8Array(digest));
  }

  async function traits(lib) {
    let found = {};
    try {
      // No api_key / simple_request: the library makes no API call. Its sampled
      // telemetry (`logging`) and beacon are switched off explicitly.
      const result = await new lib.Thumbmark({ logging: false, collect_beacon: false, exclude: SKIP }).get();
      found = (result && result.components) || {};
    } catch (_) { /* every key below then stays absent */ }
    const json = (value) => lib.stableStringify(value);
    const { canvas, webgl, audio, fonts, hardware, screen: display, system, locales } = found;
    const card = usable(hardware) && usable(hardware.videocard) ? hardware.videocard : null;
    const items = {
      a: () => (usable(canvas) && canvas.commonPixelsHash ? sha256(json(canvas)) : null),
      b: () => {
        const vendor = card && text(card.vendorUnmasked || card.vendor, 128);
        const renderer = card && text(card.rendererUnmasked || card.renderer, 200);
        const value = vendor && renderer && !vendor.includes('|') ? `${vendor}|${renderer}` : null;
        return value && value.length <= 256 ? value : null;
      },
      c: () => (usable(webgl) && webgl.commonPixelsHash
        ? sha256(json({ pixels: webgl.commonPixelsHash, version: (card && card.version) || '',
          glsl: (card && card.shadingLanguageVersion) || '' }))
        : null),
      e: () => (usable(audio) && 'sampleHash' in audio ? sha256(json(audio)) : null),
      f: () => {
        // Orientation independent; window.screen is not the Telegram viewport.
        const { width, height } = window.screen;
        const depth = usable(display) ? display.colorDepth : window.screen.colorDepth;
        const ratio = window.devicePixelRatio;
        return width > 0 && height > 0 && depth > 0 && ratio > 0
          ? `${Math.max(width, height)}x${Math.min(width, height)}x${depth}x${ratio}` : null;
      },
      g: () => (usable(system) && system.hardwareConcurrency > 0 ? String(system.hardwareConcurrency) : null),
      h: () => (usable(hardware) && Number(hardware.deviceMemory) > 0 ? String(Number(hardware.deviceMemory)) : null),
      i: () => (usable(display) && Number.isInteger(display.maxTouchPoints) && display.maxTouchPoints >= 0
        ? String(display.maxTouchPoints) : null),
      j: () => (usable(locales) ? text(locales.timezone, 64) : null),
      n: () => (usable(fonts) && Object.keys(fonts).length ? sha256(json(fonts)) : null),
    };
    const s = {};
    for (const [key, read] of Object.entries(items)) {
      try {
        const value = await read();
        if (typeof value === 'string' && value) s[key] = value;
      } catch (_) { /* single item failed: omit the key */ }
    }
    return s;
  }

  function encode(payload) {
    let binary = '';
    for (const byte of new TextEncoder().encode(JSON.stringify(payload))) binary += String.fromCharCode(byte);
    return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  async function gather() {
    const lib = window.ThumbmarkJS;
    const payload = { v: 1 };
    const k = installation();
    if (k) payload.k = k;
    payload.s = lib && typeof lib.Thumbmark === 'function' ? await traits(lib) : {};
    return { value: encode(payload), complete: Object.keys(payload.s).length >= MINIMUM };
  }

  function collect() {
    if (!pending) {
      pending = gather().catch(() => ({ value: encode({ v: 1, s: {} }), complete: false }));
      pending.then((result) => { if (!result.complete) pending = null; });
    }
    return pending.then((result) => result.value);
  }

  window.__mdx = Object.freeze({ collect });
})();
