#!/usr/bin/env node
// browser — minimal headless-Chromium automation for sandbox agents.
//
// Speaks the Chrome DevTools Protocol (CDP) directly. The only components are
// Chromium itself and this script: on first use the browser is launched as a
// detached daemon, and every invocation connects to it over Node's built-in
// WebSocket/fetch. No npm or pip dependencies, no wrapper daemon.
//
// State lives under $BROWSER_STATE_DIR (default ~/.local/state/browser):
//   endpoint   ws:// URL of the running browser's DevTools socket
//   pid        daemon process id
//   current    target id of the last-used tab
//   profile/   Chromium user-data dir; cookies and logins persist here

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

// --- state ------------------------------------------------------------------

const HOME = process.env.HOME ?? '/';
const STATE_DIR = process.env.BROWSER_STATE_DIR ?? path.join(
  process.env.XDG_STATE_HOME ?? path.join(HOME, '.local', 'state'), 'browser');
const ENDPOINT_FILE = path.join(STATE_DIR, 'endpoint');
const PID_FILE = path.join(STATE_DIR, 'pid');
const LOG_FILE = path.join(STATE_DIR, 'devtools.log');
const CURRENT_FILE = path.join(STATE_DIR, 'current');
const PROFILE_DIR = path.join(STATE_DIR, 'profile');

// Chromium binary candidates; CHROMIUM_BIN wins.
const BINS = [process.env.CHROMIUM_BIN, 'chromium', 'chromium-browser', 'google-chrome-stable'].filter(Boolean);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const readState = (f) => { try { return fs.readFileSync(f, 'utf8').trim() || null; } catch { return null; } };
const writeState = (f, s) => { fs.mkdirSync(STATE_DIR, { recursive: true }); fs.writeFileSync(f, s); };
const endpointPort = (endpoint) => new URL(endpoint).port;

async function httpJson(url, method = 'GET', timeoutMs = 5000) {
  const res = await fetch(url, { method, signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`${method} ${url}: HTTP ${res.status}`);
  return res.json();
}

function findBin() {
  for (const bin of BINS) {
    if (bin.includes('/')) {
      if (fs.existsSync(bin)) return bin;
      continue;
    }
    for (const dir of (process.env.PATH ?? '').split(path.delimiter)) {
      if (!dir) continue;
      const p = path.join(dir, bin);
      try { fs.accessSync(p, fs.constants.X_OK); return p; } catch { /* next */ }
    }
  }
  throw new Error('no Chromium binary found; install chromium or set CHROMIUM_BIN');
}

// --- browser daemon -----------------------------------------------------------

async function ensureEndpoint() {
  fs.mkdirSync(STATE_DIR, { recursive: true });
  const existing = readState(ENDPOINT_FILE);
  if (existing) {
    const alive = await httpJson(`http://127.0.0.1:${endpointPort(existing)}/json/version`, 'GET', 1000)
      .then(() => true, () => false);
    if (alive) return existing;
  }
  for (const f of [ENDPOINT_FILE, PID_FILE, CURRENT_FILE]) fs.rmSync(f, { force: true });

  const log = fs.openSync(LOG_FILE, 'w');
  const child = spawn(findBin(), [
    '--headless', '--remote-debugging-port=0', '--no-sandbox', '--disable-gpu',
    '--no-first-run', '--no-default-browser-check', '--disable-dev-shm-usage',
    `--user-data-dir=${PROFILE_DIR}`, '--window-size=1280,900', 'about:blank',
  ], { stdio: ['ignore', 'ignore', log], detached: true });
  child.on('error', () => {}); // surfaced below as an endpoint timeout
  child.unref();
  if (child.pid) writeState(PID_FILE, String(child.pid));

  // The daemon prints its DevTools URL to stderr once the port is bound.
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    const m = (readState(LOG_FILE) ?? '').match(/DevTools listening on (ws:\/\/\S+)/);
    if (m) {
      writeState(ENDPOINT_FILE, m[1]);
      return m[1];
    }
    await sleep(100);
  }
  throw new Error(`Chromium did not report a DevTools endpoint within 15s (log: ${LOG_FILE})`);
}

// --- CDP client ---------------------------------------------------------------

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.events = []; // events seen before a waiter asked for them
    this.waiters = [];
    this.pending = new Map();
    ws.addEventListener('message', (ev) => this.onMessage(String(ev.data)));
    ws.addEventListener('error', () => {}); // late errors must not crash the CLI
  }

  onMessage(data) {
    let msg;
    try { msg = JSON.parse(data); } catch { return; }
    if (msg.id && this.pending.has(msg.id)) {
      const { resolve, reject } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      msg.error ? reject(new Error(msg.error.message ?? 'CDP error')) : resolve(msg.result);
    } else if (msg.method) {
      this.events.push(msg);
      const i = this.waiters.findIndex((w) => w.method === msg.method);
      if (i >= 0) this.waiters.splice(i, 1)[0].resolve(msg);
    }
  }

  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = this.nextId++;
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  waitEvent(method, timeoutMs) {
    const backlog = this.events.findIndex((e) => e.method === method);
    if (backlog >= 0) return Promise.resolve(this.events.splice(backlog, 1)[0]);
    return new Promise((resolve, reject) => {
      let waiter;
      const timer = setTimeout(() => {
        const i = this.waiters.indexOf(waiter);
        if (i >= 0) this.waiters.splice(i, 1);
        reject(new Error(`timed out after ${timeoutMs}ms waiting for ${method}`));
      }, timeoutMs);
      waiter = { method, resolve: (m) => { clearTimeout(timer); resolve(m); } };
      this.waiters.push(waiter);
    });
  }

  close() { try { this.ws.close(); } catch { /* ignore */ } }
}

async function connect(url) {
  const ws = new WebSocket(url);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timed out connecting to ${url}`)), 5000);
    ws.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
    ws.addEventListener('error', () => { clearTimeout(timer); reject(new Error(`could not connect to ${url}`)); }, { once: true });
  });
  return new Cdp(ws);
}

// --- tabs -------------------------------------------------------------------

async function pageTargets() {
  const endpoint = await ensureEndpoint();
  const list = await httpJson(`http://127.0.0.1:${endpointPort(endpoint)}/json/list`);
  return list.filter((t) => t.type === 'page');
}

async function currentTarget() {
  const pages = await pageTargets();
  const current = readState(CURRENT_FILE);
  let target = pages.find((t) => t.targetId === current) ?? pages[0];
  if (!target) {
    const endpoint = readState(ENDPOINT_FILE);
    target = await httpJson(`http://127.0.0.1:${endpointPort(endpoint)}/json/new?url=about:blank`, 'PUT');
  }
  writeState(CURRENT_FILE, target.targetId);
  return target;
}

async function withPage(fn) {
  const target = await currentTarget();
  const cdp = await connect(target.webSocketDebuggerUrl);
  try {
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    return await fn(cdp, target);
  } finally {
    cdp.close();
  }
}

// --- in-page element collection ----------------------------------------------
// outline/click/fill share one definition of "interactive element": elements
// matching the selector that are visible. Indexes stay in DOM order, so a
// click/fill re-resolves the same index the last outline printed.

const PICK_FN = String.raw`() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0
      && r.top < window.innerHeight && r.left < window.innerWidth;
  };
  return [...document.querySelectorAll('a[href], button, input, select, textarea, summary, [role], [onclick], [contenteditable]')].filter(visible);
}`;

const OUTLINE_JS = String.raw`(els => els.map((el, i) => {
  const role = el.getAttribute('role') || (el.tagName === 'INPUT' && el.type ? 'input:' + el.type : el.tagName.toLowerCase());
  const name = (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.value
    || el.getAttribute('title') || el.textContent || (el.tagName === 'A' ? el.getAttribute('href') : ''))
    .trim().replace(/\s+/g, ' ').slice(0, 80);
  return { i, role, name };
}))(${PICK_FN})()`;

const clickJs = (n) => String.raw`(els => {
  const el = els[${n}];
  if (!el) return null;
  el.scrollIntoView({ block: 'center' });
  const r = el.getBoundingClientRect();
  return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) });
})(${PICK_FN})()`;

const focusJs = (n) => String.raw`(els => {
  const el = els[${n}];
  if (!el) return null;
  el.focus();
  if (typeof el.select === 'function') el.select();
  else {
    const s = getSelection(); s.removeAllRanges();
    const r = document.createRange(); r.selectNodeContents(el); s.addRange(r);
  }
  return true;
})(${PICK_FN})()`;

const valueJs = (n) => String.raw`(els => {
  const el = els[${n}];
  return JSON.stringify({ v: String(el ? (el.value ?? el.textContent ?? '') : '').slice(0, 120) });
})(${PICK_FN})()`;

const KEY_CODES = {
  Enter: { code: 'Enter', vk: 13, text: '\r' },
  Tab: { code: 'Tab', vk: 9 },
  Backspace: { code: 'Backspace', vk: 8 },
  Delete: { code: 'Delete', vk: 46 },
  Escape: { code: 'Escape', vk: 27 },
  Space: { code: 'Space', vk: 32, text: ' ' },
  ArrowUp: { code: 'ArrowUp', vk: 38 },
  ArrowDown: { code: 'ArrowDown', vk: 40 },
  ArrowLeft: { code: 'ArrowLeft', vk: 37 },
  ArrowRight: { code: 'ArrowRight', vk: 39 },
  PageUp: { code: 'PageUp', vk: 33 },
  PageDown: { code: 'PageDown', vk: 34 },
  Home: { code: 'Home', vk: 36 },
  End: { code: 'End', vk: 35 },
};

function normalizeUrl(url) {
  if (/^[a-z][a-z0-9+.-]*:/i.test(url)) return url;
  return `https://${url}`;
}

function indexArg(arg) {
  const n = Number(arg);
  if (!Number.isInteger(n) || n < 0) throw new Error(`element index must be a non-negative integer, got "${arg}"`);
  return n;
}

// --- commands -----------------------------------------------------------------

async function cmdOpen(url) {
  url = normalizeUrl(url);
  await withPage(async (cdp) => {
    const nav = await cdp.send('Page.navigate', { url });
    if (nav.errorText) throw new Error(nav.errorText);
    try { await cdp.waitEvent('Page.loadEventFired', 20000); } catch { /* keep going; text/eval decide */ }
    await sleep(300); // let load handlers settle
    const { result } = await cdp.send('Runtime.evaluate', {
      expression: "JSON.stringify({ t: document.title, u: location.href })", returnByValue: true,
    });
    const { t, u } = JSON.parse(result.value);
    console.log(`${t}\n${u}`);
  });
}

async function cmdEvalExpr(expression, print) {
  await withPage(async (cdp) => {
    const { result, exceptionDetails } = await cdp.send('Runtime.evaluate', {
      expression, awaitPromise: true, returnByValue: true,
    });
    if (exceptionDetails) {
      throw new Error(exceptionDetails.exception?.description ?? exceptionDetails.text ?? 'page error');
    }
    print(result.value);
  });
}

const cmdText = () => cmdEvalExpr('document.body ? document.body.innerText : document.documentElement.innerText', (v) => console.log(v ?? ''));
const cmdHtml = () => cmdEvalExpr('document.documentElement.outerHTML', (v) => console.log(v ?? ''));
const cmdUrl = () => cmdEvalExpr('location.href', (v) => console.log(v));
const cmdTitle = () => cmdEvalExpr('document.title', (v) => console.log(v));

const cmdOutline = () => cmdEvalExpr(OUTLINE_JS, (v) => {
  const els = JSON.parse(v);
  if (!els.length) return console.log('no interactive elements');
  for (const { i, role, name } of els) console.log(`[${i}] ${role} ${name ? `"${name}"` : ''}`.trimEnd());
});

async function cmdClick(nArg) {
  const n = indexArg(nArg);
  await withPage(async (cdp) => {
    const { result } = await cdp.send('Runtime.evaluate', { expression: clickJs(n), returnByValue: true });
    if (!result.value) throw new Error(`no element [${n}]; run "browser outline" first`);
    const { x, y } = JSON.parse(result.value);
    for (const params of [
      { type: 'mouseMoved', x, y },
      { type: 'mousePressed', x, y, button: 'left', clickCount: 1 },
      { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 },
    ]) await cdp.send('Input.dispatchMouseEvent', params);
    const { result: after } = await cdp.send('Runtime.evaluate', {
      expression: "JSON.stringify({ t: document.title, u: location.href })", returnByValue: true,
    });
    const { t, u } = JSON.parse(after.value);
    console.log(`${t}\n${u}`);
  });
}

async function cmdFill(nArg, text) {
  const n = indexArg(nArg);
  if (text === undefined) throw new Error('usage: browser fill <n> <text>');
  await withPage(async (cdp) => {
    const { result } = await cdp.send('Runtime.evaluate', { expression: focusJs(n), returnByValue: true });
    if (!result.value) throw new Error(`no element [${n}]; run "browser outline" first`);
    await cdp.send('Input.insertText', { text });
    const { result: after } = await cdp.send('Runtime.evaluate', { expression: valueJs(n), returnByValue: true });
    console.log(`[${n}] ${JSON.parse(after.value).v}`);
  });
}

async function cmdPress(key) {
  const known = KEY_CODES[key];
  if (!known) throw new Error(`unknown key "${key}"; available: ${Object.keys(KEY_CODES).join(', ')}`);
  await withPage(async (cdp) => {
    for (const type of ['keyDown', 'keyUp']) {
      await cdp.send('Input.dispatchKeyEvent', {
        type, key, code: known.code, windowsVirtualKeyCode: known.vk, ...(known.text ? { text: known.text } : {}),
      });
    }
  });
}

async function cmdScreenshot(file, full) {
  file = file ?? '/tmp/browser.png';
  await withPage(async (cdp) => {
    const { data } = await cdp.send('Page.captureScreenshot', {
      format: 'png', captureBeyondViewport: !!full,
    });
    fs.writeFileSync(file, Buffer.from(data, 'base64'));
    console.log(`${file} (${fs.statSync(file).size} bytes)`);
  });
}

async function cmdTabs() {
  const pages = await pageTargets();
  const current = readState(CURRENT_FILE);
  pages.forEach((t, i) => {
    console.log(`[${i}]${t.targetId === current ? '*' : ' '} ${t.title || '(untitled)'} — ${t.url}`);
  });
}

async function cmdTab(nArg) {
  const n = indexArg(nArg);
  const pages = await pageTargets();
  if (!pages[n]) throw new Error(`no tab [${n}]; run "browser tabs"`);
  writeState(CURRENT_FILE, pages[n].targetId);
  console.log(`${pages[n].title || '(untitled)'} — ${pages[n].url}`);
}

async function cmdNewtab(url) {
  url = normalizeUrl(url);
  const endpoint = await ensureEndpoint();
  const target = await httpJson(`http://127.0.0.1:${endpointPort(endpoint)}/json/new?url=${encodeURIComponent(url)}`, 'PUT');
  writeState(CURRENT_FILE, target.targetId);
  console.log(`opened ${url}`);
}

async function cmdClosetab(nArg) {
  const pages = await pageTargets();
  const n = nArg === undefined ? pages.findIndex((t) => t.targetId === readState(CURRENT_FILE)) : indexArg(nArg);
  if (!pages[n]) throw new Error(`no tab [${n}]; run "browser tabs"`);
  await fetch(`http://127.0.0.1:${endpointPort(readState(ENDPOINT_FILE))}/json/close/${pages[n].targetId}`);
  if (pages[n].targetId === readState(CURRENT_FILE)) fs.rmSync(CURRENT_FILE, { force: true });
  console.log(`closed [${n}] ${pages[n].url}`);
}

async function cmdClose() {
  const endpoint = readState(ENDPOINT_FILE);
  if (!endpoint) return console.log('browser not running');
  try {
    const cdp = await connect(endpoint);
    await cdp.send('Browser.close');
    cdp.close();
  } catch { /* stale daemon; fall through to SIGTERM */ }
  const pid = Number(readState(PID_FILE));
  try { if (pid) process.kill(pid, 'SIGTERM'); } catch { /* already gone */ }
  for (const f of [ENDPOINT_FILE, PID_FILE, CURRENT_FILE]) fs.rmSync(f, { force: true });
  console.log('browser stopped');
}

// --- entry point ----------------------------------------------------------------

const USAGE = `browser — headless Chromium automation via CDP (no external packages)

The browser runs as a detached daemon; cookies persist in ~/.local/state/browser/profile.

Commands:
  open <url>            navigate the current tab (waits for page load)
  text                  page text content
  html                  full HTML
  outline               numbered visible interactive elements (click/fill targets)
  click <n>             click outlined element <n> (real mouse events)
  fill <n> <text>       focus outlined element <n>, select all, insert <text>
  press <key>           send a key: ${Object.keys(KEY_CODES).join(', ')}
  screenshot [path]     PNG to <path> (default /tmp/browser.png); --full = beyond viewport
  eval <js>             run JS in the page; string results print raw, others as JSON
  url | title           current tab URL / title
  tabs                  list tabs (* = current)
  tab <n>               switch to tab <n>
  newtab <url>          open a new tab and make it current
  closetab [n]          close tab <n> (default: current)
  close                 stop the browser daemon

Typical flow:
  browser open example.com
  browser outline
  browser fill 2 "search terms"
  browser press Enter
  browser text
  browser screenshot /workspace/page.png`;

async function main() {
  const [command, ...rest] = process.argv.slice(2);
  const flags = rest.filter((a) => a.startsWith('--'));
  const args = rest.filter((a) => !a.startsWith('--'));
  switch (command) {
    case undefined:
    case '--help':
    case '-h':
    case 'help':
      return console.log(USAGE);
    case 'open': return cmdOpen(args[0] ?? (() => { throw new Error('usage: browser open <url>'); })());
    case 'text': return cmdText();
    case 'html': return cmdHtml();
    case 'outline': return cmdOutline();
    case 'click': return cmdClick(args[0] ?? (() => { throw new Error('usage: browser click <n>'); })());
    case 'fill': return cmdFill(args[0], args.slice(1).join(' ') || undefined);
    case 'press': return cmdPress(args[0] ?? (() => { throw new Error('usage: browser press <key>'); })());
    case 'screenshot': return cmdScreenshot(args[0], flags.includes('--full'));
    case 'eval': return cmdEvalExpr(args.join(' ') || (() => { throw new Error('usage: browser eval <js>'); })(), (v) => {
      if (v === undefined) return console.log('undefined');
      console.log(typeof v === 'string' ? v : JSON.stringify(v, null, 2));
    });
    case 'url': return cmdUrl();
    case 'title': return cmdTitle();
    case 'tabs': return cmdTabs();
    case 'tab': return cmdTab(args[0]);
    case 'newtab': return cmdNewtab(args[0] ?? (() => { throw new Error('usage: browser newtab <url>'); })());
    case 'closetab': return cmdClosetab(args[0]);
    case 'close': return cmdClose();
    default:
      console.error(USAGE);
      throw new Error(`unknown command "${command}"`);
  }
}

main().catch((err) => {
  console.error(`browser: ${err.message}`);
  process.exitCode = 1;
});
