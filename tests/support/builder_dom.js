// A small DOM, just big enough to run pc/theme_builder.html's script in node.
//
// Why it exists: the reviews of fp/builder-look and fp/edit-reset found bugs
// that only showed in a real browser (a hex box's error outliving its value,
// "Loaded" over a refused code, live regions rewritten on every render, an
// older tab closing itself with a draft in it), while the tests of the time
// matched strings in the script. CI has node but no browser, and the repo
// takes no new dependency, so there is no jsdom: this is the part of the DOM
// the page uses and nothing else. If the page starts using more, this is the
// file to grow; an unknown selector throws rather than quietly matching
// nothing.
//
// Run as: node builder_dom.js <case.json>. The case gives the markup tree
// (parsed in Python), the page script, the server's answers, and a scenario,
// an async function body with the helpers below in scope. The scenario's
// return value is printed as JSON.
'use strict';
const fs = require('fs');
const spec = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

let active = null;
const all = [];

class Classes {
  constructor(el) { this.el = el; }
  _get() { return this.el.className ? this.el.className.split(/\s+/).filter(Boolean) : []; }
  contains(c) { return this._get().includes(c); }
  add(c) { if (!this.contains(c)) this.el.className = this._get().concat([c]).join(' '); }
  remove(c) { this.el.className = this._get().filter((x) => x !== c).join(' '); }
  toggle(c, on) { if (on === undefined) on = !this.contains(c); if (on) this.add(c); else this.remove(c); return on; }
}

class Node {
  constructor(tag) {
    this.tagName = (tag || '#text').toUpperCase();
    this.children = []; this.parentNode = null;
    this.attrs = {}; this.dataset = {}; this.style = {};
    this.listeners = {}; this._text = ''; this.textWrites = 0;
    this.className = ''; this.classList = new Classes(this);
    this.value = ''; this.disabled = false;
    all.push(this);
  }
  get id() { return this.attrs.id || ''; }
  set id(v) { this.attrs.id = v; }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null; return c; }
  get textContent() { return this.tagName === '#TEXT' ? this._text : this._text + this.children.map((c) => c.textContent).join(''); }
  // Every write counts, the same text included: a live region re-announces
  // on a replaced text node, which is what the review measured.
  set textContent(v) { this.children = []; this._text = String(v); this.textWrites++; }
  setAttribute(k, v) { v = String(v); if (k === 'class') this.className = v; else this.attrs[k] = v; if (k.startsWith('data-')) this.dataset[k.slice(5)] = v; }
  getAttribute(k) { return k === 'class' ? this.className : (k in this.attrs ? this.attrs[k] : null); }
  removeAttribute(k) { delete this.attrs[k]; }
  set htmlFor(v) { this.attrs.for = v; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  fire(t, extra) { const e = Object.assign({ target: this, preventDefault() {} }, extra || {}); (this.listeners[t] || []).forEach((f) => f(e)); }
  click() { if (!this.disabled) this.fire('click'); }
  focus() { if (active && active !== this) { const was = active; active = null; was.fire('blur'); } active = this; }
  blur() { if (active === this) { active = null; this.fire('blur'); } }
  contains(o) { for (let n = o; n; n = n.parentNode) if (n === this) return true; return false; }
  descendants() { const out = []; const walk = (n) => n.children.forEach((c) => { out.push(c); walk(c); }); walk(this); return out; }
  querySelectorAll(sel) { return select(sel, this); }
  querySelector(sel) { return select(sel, this)[0] || null; }
}

function simple(tok, n) {
  if (tok.startsWith('#')) return n.id === tok.slice(1);
  if (tok.startsWith('.')) return n.classList.contains(tok.slice(1));
  const m = /^\[data-([a-z]+)\]$/.exec(tok);
  if (m) return m[1] in n.dataset;
  if (/^[a-z]+$/.test(tok)) return n.tagName === tok.toUpperCase();
  throw new Error('builder_dom.js does not know the selector ' + tok);
}
function select(sel, scope) {
  const pool = scope.descendants();
  const hit = new Set();
  sel.split(',').map((s) => s.trim().split(/\s+/)).forEach((chain) => {
    pool.forEach((n) => {
      if (!simple(chain[chain.length - 1], n)) return;
      let i = chain.length - 2;
      for (let a = n.parentNode; a && i >= 0; a = a.parentNode) if (simple(chain[i], a)) i--;
      if (i < 0) hit.add(n);
    });
  });
  return pool.filter((n) => hit.has(n));
}

function build(t) {
  if (typeof t === 'string') { const n = new Node(); n._text = t; return n; }
  const n = new Node(t.tag);
  Object.entries(t.attrs).forEach(([k, v]) => n.setAttribute(k, v));
  if ('value' in t.attrs) n.value = t.attrs.value;
  t.children.forEach((c) => n.appendChild(build(c)));
  return n;
}

const root = new Node('html');
root.appendChild(build(spec.markup));
const document = {
  hidden: false,
  documentElement: root,
  listeners: {},
  get activeElement() { return active; },
  getElementById(id) { return root.descendants().find((n) => n.id === id) || null; },
  createElement(tag) { return new Node(tag); },
  createElementNS(ns, tag) { return new Node(tag); },
  createTextNode(t) { const n = new Node(); n._text = String(t); return n; },
  querySelectorAll(sel) { return select(sel, root); },
  querySelector(sel) { return select(sel, root)[0] || null; },
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
  fire(t, extra) { const e = Object.assign({ preventDefault() { e.prevented = true; } }, extra || {}); (this.listeners[t] || []).forEach((f) => f(e)); return e; },
};

// Timers on a clock the scenario moves: advance(ms) runs what falls due.
let now = 0, nextId = 1;
const timers = new Map();
function setTimeout_(f, ms) { const id = nextId++; timers.set(id, { at: now + (ms || 0), f, every: 0 }); return id; }
function setInterval_(f, ms) { const id = nextId++; timers.set(id, { at: now + ms, f, every: ms }); return id; }
function clearTimer(id) { timers.delete(id); }

// The server: answers come from spec.answers by path, or from a function
// the scenario installs with answer(path, fn). Every request is kept.
const requests = [];
const answers = {};
// 'held': a reply kept back for the scenario to release, for one the page
// asks for as it loads, before a scenario can hold() it: held[path] lists
// them in order, each a hold(). The Save-before-state fix (NN-3) needed a
// GET /api/state that answers late rather than never.
const held = {};
Object.entries(spec.answers || {}).forEach(([p, a]) => { answers[p] = () => a; });
function fetch_(path, opts) {
  opts = opts || {};
  const req = { path, method: opts.method || 'GET', headers: opts.headers || {}, body: opts.body ? JSON.parse(opts.body || 'null') : null };
  requests.push(req);
  const a = (answers[path] || (() => ({ status: 200, body: { ok: true } })))(req);
  // An answer can be a promise, from hold(), so a scenario decides when a
  // reply lands and in what order against the others.
  const settle = (x) => {
    if (x === 'fail') return Promise.reject(new Error('network'));
    // 'hold': a reply that never comes, for one the page asks for as it
    // loads, before a scenario can hold() it.
    if (x === 'hold') return new Promise(() => {});
    return Promise.resolve({ status: x.status, json: () => Promise.resolve(JSON.parse(JSON.stringify(x.body || {}))) });
  };
  let reply;
  if (a === 'held') { const h = hold(); (held[path] = held[path] || []).push(h); reply = h.p.then(settle); }
  else reply = a && typeof a.then === 'function' ? a.then(settle) : settle(a);
  // An aborted request rejects, as a browser's fetch does, whether or not
  // its reply was ever coming. node's own AbortController drives it.
  if (!opts.signal) return reply;
  req.limited = true;
  return new Promise((resolve, reject) => {
    opts.signal.addEventListener('abort', () => reject(new Error('aborted')));
    reply.then(resolve, reject);
  });
}

// closed counts the closes the browser allowed (a real window.closed is a
// boolean, and either reads as true here); calls counts every ask. A
// scenario sets refuse = true to be a browser that will not close a tab
// that has a history behind it.
const win = { closed: 0, calls: 0, refuse: false, listeners: {}, close() { this.calls++; if (!this.refuse) this.closed++; }, addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); } };

// The clipboard takes every write and keeps it, so a test can read what a
// copy button copied. refuse = true makes it reject, as a browser does
// without permission or outside a secure context.
const clipboard = { writes: [], refuse: false, writeText(t) { if (this.refuse) return Promise.reject(new Error('denied')); this.writes.push(String(t)); return Promise.resolve(); } };
// What a copy's fallback selected, by the node it selected.
const selection = { node: null };
document.createRange = () => ({ selectNodeContents(n) { this.node = n; } });
// A reply the scenario releases: answer(path, () => h.p), then h.release(x)
// with an answer ({status, body} or 'fail').
function hold() { let release; const p = new Promise((r) => { release = r; }); return { p, release }; }

const sandbox = {
  document, window: win, fetch: fetch_, setTimeout: setTimeout_, setInterval: setInterval_,
  clearTimeout: clearTimer, clearInterval: clearTimer, navigator: { clipboard },
  getSelection: () => ({ removeAllRanges() { selection.node = null; }, addRange(r) { selection.node = r.node; } }),
  btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
  atob: (s) => { if (/[^A-Za-z0-9+/=]/.test(s)) throw new Error('bad'); return Buffer.from(s, 'base64').toString('binary'); },
};

async function flush() { for (let i = 0; i < 20; i++) await Promise.resolve(); }
async function advance(ms) {
  const end = now + ms;
  for (;;) {
    await flush();
    let due = null;
    for (const [id, t] of timers) if (t.at <= end && (!due || t.at < due[1].at)) due = [id, t];
    if (!due) break;
    const [id, t] = due;
    now = t.at;
    if (t.every) t.at += t.every; else timers.delete(id);
    t.f();
  }
  now = end;
  await flush();
}
const $ = (id) => document.getElementById(id);
function type(id, text) { const e = $(id); e.focus(); e.value = text; e.fire('input'); }
function pick(id, hex) { const e = $(id); e.value = hex; e.fire('input'); }
function preset(name) {
  const b = document.querySelectorAll('#presets button').find((x) => x.querySelector('.pn').textContent === name);
  if (!b) throw new Error('no preset ' + name);
  b.focus(); b.click();
}
function answer(path, fn) { answers[path] = fn; }
function liveWrites() {
  const out = {};
  all.filter((n) => n.attrs['aria-live'] && n.id).forEach((n) => { out[n.id] = n.textWrites; });
  return out;
}

const helpers = { $, document, window: win, advance, flush, type, pick, preset, answer, requests, liveWrites, clipboard, selection, hold, held };
const names = Object.keys(sandbox);
// The page script, as the browser would run it, against this DOM.
new Function(...names, spec.script)(...names.map((k) => sandbox[k]));
const scenario = new Function(...Object.keys(helpers), 'return (async () => {' + spec.scenario + '})();');
scenario(...Object.values(helpers)).then((r) => {
  process.stdout.write(JSON.stringify(r === undefined ? null : r));
}, (e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
