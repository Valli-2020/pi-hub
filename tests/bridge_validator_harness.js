// Runs the dashboard's PHF (frame host) message validator under node with DOM
// stubs, feeding it hostile and random messages.  Driven by
// tests/test_bridge_validator.py; prints one JSON line per failed check.
'use strict';
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync(process.argv[2], 'utf8');
const start = html.indexOf('const PHF = (function(){');
const end = html.indexOf('/* ── Permission descriptions (consent dialog)');
if (start < 0 || end < 0) { console.log(JSON.stringify({ fail: 'cannot locate PHF block' })); process.exit(0); }
const block = html.slice(start, end);

const failures = [];
const fail = (name, detail) => failures.push({ name, detail: String(detail).slice(0, 300) });
const rec = { virt: [], toasts: [], fetches: [], posts: [], opened: [], nav: [] };

const okHref = h => typeof h === 'string' && /^(https?:\/\/[^\s\\"'<>]+|\/(?!\/)[^\s\\"'<>]*)$/i.test(h);
const okAction = a => typeof a === 'string' && /^[A-Za-z0-9._~-]+(\/[A-Za-z0-9._~-]+)*$/.test(a) && !a.split('/').includes('..');

function fakeEl(){
  const el = { children: [], dataset: {}, style: {}, className: '', hidden: false,
    after(){}, remove(){}, appendChild(c){ this.children.push(c); return c; }, insertBefore(c){ this.children.push(c); return c; },
    setAttribute(){}, addEventListener(){}, querySelector(){ return fakeEl(); }, parentNode: null, set innerHTML(v){}, get innerHTML(){ return ''; } };
  el.parentNode = { insertBefore(){} };
  return el;
}
// The ph.open fallback dialog: stable elements so the checks can inspect them.
function dialogEl(){
  const el = fakeEl(); el.attrs = {}; el.open = false; el.textContent = '';
  el.setAttribute = (k, v) => { el.attrs[k] = String(v); };
  el.removeAttribute = k => { delete el.attrs[k]; };
  el.showModal = () => { el.open = true; };
  el.close = () => { el.open = false; };
  return el;
}
const DLG = { '#phopen': dialogEl(), '#po-sub': dialogEl(), '#po-url': dialogEl(), '#po-link': dialogEl(), '#po-cancel': dialogEl() };
const activation = { isActive: true };
// Any write to location (navigating the dashboard) is recorded as a failure.
const location = {};
for (const k of ['href', 'pathname', 'hash', 'search', 'host', 'protocol'])
  Object.defineProperty(location, k, { get: () => (k === 'search' ? '' : ''), set: v => rec.nav.push(k + '=' + v) });
for (const k of ['assign', 'replace', 'reload']) location[k] = v => rec.nav.push(k + '(' + v + ')');
const ctx = {
  console, JSON, Math, Date, Object, Array, Number, String, Map, Set, Promise, URL, isFinite, setTimeout, performance: { now: () => Date.now() }, setInterval: () => 1,
  location, sessionStorage: { getItem: () => null, setItem(){} },
  navigator: { userActivation: activation },
  window: { USER: { role: 'admin' }, open: (...a) => { rec.opened.push(a); return null; }, location },
  document: { addEventListener(){}, createElement: fakeEl, documentElement: { dataset: { theme: 'dark' } } },
  getComputedStyle: () => ({ getPropertyValue: () => '#111' }),
  MessageChannel: function(){ this.port1 = {}; this.port2 = {}; },
  $: sel => DLG[sel] || fakeEl(), $$: () => [], esc: s => String(s), toast: (m, k) => rec.toasts.push([m, k]),
  sanitizeSvg: () => '', S: { view: 'hosts' }, showView(){},
  apiFetch: () => Promise.resolve({ status: 200, json: () => Promise.resolve({}) }),
  fetch: (url, o) => { rec.fetches.push([url, o]); return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{"ok":1}') }); },
  PH: { safe: false, okHref, okAction,
        setVirtual: (...a) => rec.virt.push(a), dropVirtual(){}, rnode: () => '' },
};
vm.createContext(ctx);
vm.runInContext(block + '\nthis.PHF = PHF;', ctx);
const PHF = ctx.PHF;
const VM_PROTO = vm.runInContext('Object.getPrototypeOf({})', ctx);   // objects the validator builds live in the vm realm

const spec = { plugin: 'p', id: 'f', surfaces: [], height: 200,
               renders: ['hosts.card.badges', 'header.pill'], keyed: { 'hosts.card.badges': true, 'header.pill': false },
               reads: ['hosts.status'] };
function mkInst(){
  const inst = PHF._create(spec, { type: 'widget', view: 'hosts' });
  inst.iframe = fakeEl(); inst.port = { postMessage: m => rec.posts.push(m), close(){} };
  inst.token = 'tok'; inst.mountedAt = Date.now();
  return inst;
}
let inst = mkInst();
const send = m => { try { PHF._onMessage(inst, m); return null; } catch (e) { return e; } };
const fresh = () => { PHF._insts.clear(); inst = mkInst(); rec.virt.length = 0; rec.posts.length = 0; rec.fetches.length = 0; rec.toasts.length = 0;
                      rec.opened.length = 0; DLG['#phopen'].close(); DLG['#po-link'].attrs = {}; activation.isActive = true; inst.visible = true; };

// ── invariant for anything that reaches the renderer ─────────────────────
const KNOWN = new Set(['text','badge','status','stat','gauge','kv','link','button','empty','stack','columns','section','table']);
function checkClean(n, depth, count, where){
  if (n === null || typeof n !== 'object') return;
  if (Array.isArray(n)) { n.forEach(c => checkClean(c, depth, count, where)); return; }
  if (Object.getPrototypeOf(n) !== VM_PROTO && Object.getPrototypeOf(n) !== Object.prototype) fail('non-plain object reached renderer', where);
  if (n.type !== undefined) {
    count.n++;
    if (!KNOWN.has(n.type)) fail('unknown node type reached renderer: ' + n.type, where);
    if (depth > 8) fail('depth', where);
  }
  for (const [k, v] of Object.entries(n)) {
    if (typeof v === 'string' && v.length > 500) fail('string > 500 in ' + k, where);
    if (typeof v === 'function') fail('function in node', where);
    if (k === 'href' && !okHref(v)) fail('bad href reached renderer: ' + v, where);
    if (k === 'action' && n.type === 'button' && !okAction(v)) fail('bad action reached renderer: ' + v, where);
    if (k === 'fields') {                        // form-field descriptors also carry a `type`, but it is not a node type
      (v || []).forEach(f => { if (['text','password','number','checkbox','select'].indexOf(f.type) < 0) fail('bad field type reached renderer: ' + f.type, where);
                               if (!/^[A-Za-z_][A-Za-z0-9_]{0,31}$/.test(f.name)) fail('bad field name reached renderer', where); });
      continue;
    }
    checkClean(v, depth + 1, count, where);
  }
}

// ── directed cases ───────────────────────────────────────────────────────
const R = (o) => Object.assign({ op: 'render', slot: 'hosts.card.badges', key: 'h1', node: { type: 'badge', text: 'x' } }, o);
function accepts(name, m){ fresh(); const e = send(m); if (e) fail(name + ' threw', e.message); else if (!rec.virt.length) fail(name + ' should be accepted', JSON.stringify(m).slice(0, 100)); }
function rejects(name, m){ fresh(); const e = send(m); if (e) fail(name + ' threw', e.message); else if (rec.virt.length) fail(name + ' should be rejected', JSON.stringify(m).slice(0, 100)); }

accepts('plain badge', R({}));
accepts('delete (null node)', R({ node: null }) ) ;
fresh(); send(R({})); if (rec.virt.length !== 1) fail('virt recorded', rec.virt.length);
rejects('unknown op handled', { op: 'eval', code: 'x' });
rejects('slot not declared', R({ slot: 'settings.card' }));
rejects('slot not a string', R({ slot: ['hosts.card.badges'] }));
rejects('keyed slot without key', R({ key: null }));
rejects('key with html', R({ key: '<img src=x onerror=alert(1)>' }));
rejects('key too long', R({ key: 'k'.repeat(200) }));
rejects('singleton with key', R({ slot: 'header.pill', key: 'x' }));
accepts('singleton without key', R({ slot: 'header.pill', key: null }));
rejects('unknown node type', R({ node: { type: 'script', text: 'x' } }));
rejects('iframe node', R({ node: { type: 'iframe', src: 'javascript:alert(1)' } }));
rejects('img node', R({ node: { type: 'img', src: 'x' } }));
rejects('javascript: link', R({ node: { type: 'link', text: 'x', href: 'javascript:alert(1)' } }));
rejects('data: link', R({ node: { type: 'link', text: 'x', href: 'data:text/html,<script>alert(1)</script>' } }));
rejects('protocol-relative link', R({ node: { type: 'link', text: 'x', href: '//evil.example/x' } }));
rejects('button with path traversal', R({ node: { type: 'button', label: 'x', action: '../config/full' } }));
rejects('button with absolute action', R({ node: { type: 'button', label: 'x', action: '/api/config/full' } }));
rejects('button with query', R({ node: { type: 'button', label: 'x', action: 'a?b=1' } }));
accepts('good button', R({ node: { type: 'button', label: 'Go', action: 'do/it', confirm: 'sure?' } }));
rejects('NaN gauge', R({ node: { type: 'gauge', pct: NaN } }));
rejects('string gauge', R({ node: { type: 'gauge', pct: 'abc' } }));
rejects('table nested in table cell', R({ node: { type: 'table', columns: [{ key: 'a' }], rows: [{ a: { type: 'table', columns: [{ key: 'b' }], rows: [] } }] } }));
rejects('table with 13 columns', R({ node: { type: 'table', columns: Array.from({ length: 13 }, (_, i) => ({ key: 'c' + i })), rows: [] } }));
rejects('table with 501 rows', R({ node: { type: 'table', columns: [{ key: 'a' }], rows: Array.from({ length: 501 }, () => ({ a: 1 })) } }));
rejects('column key with spaces', R({ node: { type: 'table', columns: [{ key: 'a b' }], rows: [] } }));
rejects('row action that is not a button', R({ node: { type: 'table', columns: [{ key: 'a' }], rows: [], row_actions: [{ type: 'text', text: 'x' }] } }));
let deep = { type: 'text', text: 'x' }; for (let i = 0; i < 9; i++) deep = { type: 'stack', children: [deep] };
rejects('depth 9', R({ node: deep }));
rejects('more than 500 nodes', R({ node: { type: 'stack', children: Array.from({ length: 100 }, () => ({ type: 'stack', children: Array.from({ length: 6 }, () => ({ type: 'text', text: 'x' })) })) } }));
rejects('node JSON over 32 KB', R({ node: { type: 'stack', children: Array.from({ length: 90 }, () => ({ type: 'text', text: 'y'.repeat(499) })) } }));
rejects('children not an array', R({ node: { type: 'stack', children: 'x' } }));
rejects('kv row wrong shape', R({ node: { type: 'kv', rows: [['a']] } }));
rejects('node is an array', R({ node: [1, 2] }));
rejects('node is a string', R({ node: 'x' }));
rejects('node from a prototype-carrying object', R({ node: Object.create({ type: 'text', text: 'x' }) }));
rejects('message from a class instance', new (class M { constructor(){ this.op = 'render'; } })());
rejects('array message', [R({})]);
rejects('null message', null);
rejects('string message', 'render');
// (functions cannot cross a MessageChannel — structured clone refuses them — so no case for those)
{
  fresh(); send(R({ node: JSON.parse(JSON.stringify({ type: 'text', text: 'a'.repeat(5000), evil: '<script>' }).replace('{', '{"__proto__":{"x":1},')) }));
  const n = rec.virt[0] && rec.virt[0][5];
  if (!n || n.text.length !== 500 || 'evil' in n || Object.keys(n).includes('__proto__')) fail('text truncated to 500, unknown fields (incl. own __proto__) dropped', JSON.stringify(n || null).slice(0, 120));
  fresh(); send(R({ node: JSON.parse('{"type":"badge","text":"x","__proto__":{"polluted":1}}') }));
  if ({}.polluted) fail('prototype pollution', 'Object.prototype.polluted set');
  const k = R({ key: '__proto__' }); fresh(); const e = send(k); if (e) fail('__proto__ key threw', e.message);
}

// ── call / read ───────────────────────────────────────────────────────────
function callTest(name, m, expectFetch, expectStatus){
  fresh(); const e = send(m);
  if (e) { fail(name + ' threw', e.message); return; }
  return new Promise(r => setTimeout(() => {
    if (expectFetch && !rec.fetches.length) fail(name + ' should fetch', JSON.stringify(rec.posts));
    if (!expectFetch && rec.fetches.length) fail(name + ' must NOT fetch', rec.fetches[0][0]);
    if (expectStatus) { const res = rec.posts.find(p => p.op === 'result'); if (!res || res.status !== expectStatus) fail(name + ' status', JSON.stringify(rec.posts)); }
    r();
  }, 10));
}
(async () => {
  await callTest('good call', { op: 'call', id: 1, route: 'samples', method: 'GET', body: null }, true);
  { fresh(); send({ op: 'call', id: 1, route: 'a/b', method: 'POST', body: { x: 1 } }); await new Promise(r => setTimeout(r, 10));
    const [url, o] = rec.fetches[0] || [];
    if (url !== '/api/plugin/p/a/b') fail('call URL is pinned to the plugin', url);
    if (!o || o.headers.Authorization !== 'Frame tok' || o.credentials !== 'omit') fail('call uses the frame token, no credentials', JSON.stringify(o)); }
  for (const route of ['../config/full', '/api/config/full', 'a/../b', 'a//b', './x', 'a b', 'a?x=<', 'http://evil/x', '', 'a\\b', '%2e%2e/x', 'a#b', 'x'.repeat(200), 'a/./b'])
    await callTest('call route ' + JSON.stringify(route).slice(0, 30), { op: 'call', id: 1, route, method: 'GET' }, false, 400);
  for (const method of ['TRACE', 'CONNECT', 'get', 5, ['GET'], {}])
    await callTest('call method ' + JSON.stringify(method), { op: 'call', id: 1, route: 'x', method }, false, 400);
  await callTest('big body', { op: 'call', id: 1, route: 'x', method: 'POST', body: { a: 'x'.repeat(70000) } }, false, 413);
  for (const id of [0, -1, 1.5, '1', null, undefined, NaN])
    await callTest('call id ' + String(id), { op: 'call', id, route: 'x' }, false);
  await callTest('read declared', { op: 'read', id: 1, name: 'hosts.status' }, true);
  { fresh(); send({ op: 'read', id: 1, name: 'hosts.status' }); await new Promise(r => setTimeout(r, 10)); if (rec.fetches[0][0] !== '/api/hosts/status') fail('read maps to the fixed path', rec.fetches[0][0]); }
  for (const name of ['proxmox.containers', 'config.full', '/api/config/full', '../x', 5, null, ['hosts.status']])
    await callTest('read undeclared ' + JSON.stringify(name), { op: 'read', id: 1, name }, false, 403);

  // rate limits and misbehaviour
  fresh(); for (let i = 0; i < 40; i++) send({ op: 'call', id: i + 1, route: 'x' });
  await new Promise(r => setTimeout(r, 20));
  if (rec.fetches.length > 21) fail('call rate limit (burst 20)', rec.fetches.length);
  if (inst.iframe) fail('call flood kills the frame', 'still mounted');
  fresh(); for (let i = 0; i < 30; i++) send(R({ key: 'k' + i }));
  if (rec.virt.length > 11) fail('render rate limit (burst 10)', rec.virt.length);
  fresh(); for (let i = 0; i < 5; i++) send({ op: 'toast', msg: 'x' + i });
  if (rec.toasts.length !== 1) fail('toast rate limit (1 per 5 s)', rec.toasts.length);
  fresh(); send({ op: 'toast', msg: '<b>x</b>'.repeat(100) });
  if (rec.toasts[0] && rec.toasts[0][0].length > 220) fail('toast text capped', rec.toasts[0][0].length);
  fresh(); send({ op: 'bogus' }); send('x'); send(null);
  if (inst.iframe) fail('three violations kill the frame', 'still mounted');
  fresh(); send({ op: 'resize', h: 1e9 }); if (inst.h > 1200) fail('resize clamped for widgets', inst.h);
  fresh(); send({ op: 'resize', h: -5 }); if (inst.h < 40) fail('resize lower bound', inst.h);
  fresh(); send({ op: 'resize', h: 'x' }); send({ op: 'resize', h: NaN }); send({ op: 'resize', h: Infinity });
  if (inst.iframe) fail('bad resize values count as violations', 'still mounted');

  // ── ph.open ─────────────────────────────────────────────────────────────
  const result = () => { const r = rec.posts.find(p => p.op === 'result'); return r && r.data; };
  const opens = (name, url, want) => {
    fresh(); const e = send({ op: 'open', id: 1, url });
    if (e) return fail('open ' + name + ' threw', e.message);
    const r = result();
    if (!r) return fail('open ' + name + ': no reply', JSON.stringify(rec.posts));
    if (want === undefined) {
      if (r.ok !== false || rec.opened.length || DLG['#phopen'].open) fail('open ' + name + ' must be refused', JSON.stringify([r, rec.opened]));
    } else {
      const got = rec.opened[0];
      if (r.ok !== true || !got || got[0] !== want || got[1] !== '_blank' || got[2] !== 'noopener,noreferrer')
        fail('open ' + name + ' should open ' + want + ' with noopener,noreferrer', JSON.stringify([r, rec.opened]));
    }
    if (!inst.iframe) fail('open ' + name + ': a refused link must not count as a violation', 'frame killed');
  };
  opens('https', 'https://example.local/a?b=1#c', 'https://example.local/a?b=1#c');
  opens('http with port', 'http://192.0.2.10:8080/x', 'http://192.0.2.10:8080/x');
  opens('upper-case scheme is normalised', 'HTTPS://Example.Local/', 'https://example.local/');
  opens('exactly 2000 chars', 'https://example.local/' + 'a'.repeat(2000 - 22), 'https://example.local/' + 'a'.repeat(2000 - 22));
  for (const [name, url] of [
    ['javascript:', 'javascript:window.top.__pwned=1'], ['JavaScript: mixed case', 'JaVaScRiPt:alert(1)'],
    ['javascript: with leading space', ' javascript:alert(1)'], ['javascript: with a tab inside', 'java\tscript:alert(1)'],
    ['javascript: with a newline inside', 'java\nscript:alert(1)'], ['vbscript:', 'vbscript:msgbox(1)'],
    ['data:', 'data:text/html,<script>alert(1)</script>'], ['file:', 'file:///etc/passwd'], ['blob:', 'blob:https://example.local/x'],
    ['about:blank', 'about:blank'], ['ftp:', 'ftp://example.local/x'], ['ws:', 'ws://example.local/'], ['mailto:', 'mailto:a@example.local'],
    ['relative path', '/api/config/full'], ['relative name', 'x'], ['dot-relative', '../x'], ['protocol-relative', '//example.local/x'],
    ['query only', '?pwned=1'], ['fragment only', '#x'], ['user + password', 'https://user:pw@example.local/'],
    ['user only', 'https://user@example.local/'], ['password only', 'https://:pw@example.local/'], ['empty', ''],
    ['2001 chars', 'https://example.local/' + 'a'.repeat(2001 - 22)], ['http without host', 'http://'],
    ['number', 5], ['object', { href: 'https://example.local/' }], ['array', ['https://example.local/']], ['null', null], ['missing', undefined],
  ]) opens(name, url);
  fresh(); for (const id of [0, -1, 1.5, '1', null]) send({ op: 'open', id, url: 'https://example.local/' });
  if (rec.opened.length) fail('open with a bad id must not open', JSON.stringify(rec.opened));
  fresh(); send({ op: 'open', id: 1, url: 'https://example.local/1' }); send({ op: 'open', id: 2, url: 'https://example.local/2' });
  if (rec.opened.length !== 1 || (rec.posts.find(p => p.id === 2) || {}).data.ok !== false) fail('open rate limit (1 per second)', JSON.stringify(rec.posts));
  if (!inst.iframe) fail('a rate-limited open is not a violation', 'frame killed');
  await new Promise(r => setTimeout(r, 1050));
  rec.opened.length = 0; send({ op: 'open', id: 3, url: 'https://example.local/3' });
  if (rec.opened.length !== 1) fail('open allowed again after a second', JSON.stringify(rec.opened));
  fresh(); inst.visible = false; send({ op: 'open', id: 1, url: 'https://example.local/' });
  if (rec.opened.length || result().ok !== false) fail('a hidden frame cannot open links', JSON.stringify(rec.posts));
  // no user activation: the popup would be blocked, so the dialog shows a real link
  fresh(); activation.isActive = false; send({ op: 'open', id: 1, url: 'https://example.local/x y' });
  if (rec.opened.length) fail('without activation window.open is not called', JSON.stringify(rec.opened));
  if (!DLG['#phopen'].open || DLG['#po-link'].attrs.href !== 'https://example.local/x%20y' || DLG['#po-url'].textContent !== 'https://example.local/x%20y')
    fail('fallback dialog shows the normalised URL and links to it', JSON.stringify([DLG['#phopen'].open, DLG['#po-link'].attrs, DLG['#po-url'].textContent]));
  if (result().ok !== true) fail('fallback dialog resolves ok', JSON.stringify(rec.posts));
  rec.posts.length = 0; await new Promise(r => setTimeout(r, 1050));
  send({ op: 'open', id: 2, url: 'https://example.local/other' });
  if (DLG['#po-link'].attrs.href !== 'https://example.local/x%20y' || result().ok !== false) fail('an open dialog cannot be swapped to another URL', JSON.stringify(DLG['#po-link'].attrs));
  if (rec.nav.length) fail('ph.open navigated the dashboard', JSON.stringify(rec.nav));

  // ── fuzz ────────────────────────────────────────────────────────────────
  let seed = 12345; const rnd = () => (seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296;
  const pick = a => a[Math.floor(rnd() * a.length)];
  const words = ['text','badge','status','stat','gauge','kv','link','button','empty','stack','columns','section','table','script','iframe','img',
    '__proto__','constructor','prototype','toString','', 'x', '<script>alert(1)</script>', 'javascript:alert(1)', '../..', 'a'.repeat(600), '\u0000', '‮', 'admin', 'select', 'GET', 'POST'];
  const keys = ['type','text','tone','title','state','value','unit','label','pct','href','action','method','confirm','style','caps','fields','name','options',
    'rows','columns','key','align','mono','row_actions','children','dir','_id','default','required','placeholder','__proto__','constructor','op','id','slot','node','route','body'];
  function gen(d){
    const r = rnd();
    if (d > 7 || r < 0.25) return pick([0, 1, -1, 1e308, NaN, Infinity, 1.5, true, false, null, undefined, pick(words), pick(words) + pick(words), []]);
    if (r < 0.45) return Array.from({ length: Math.floor(rnd() * 5) }, () => gen(d + 1));
    const o = {};
    if (rnd() < 0.7) o.type = pick(words);
    for (let i = 0, n = Math.floor(rnd() * 7); i < n; i++) o[pick(keys)] = gen(d + 1);
    return o;
  }
  // Mostly-valid nodes with random mutations, so the fuzzer gets past the type check.
  const T = ['text','badge','status','stat','gauge','kv','link','button','empty','stack','columns','section','table'];
  function genNode(d){
    const t = pick(T), o = { type: t };
    const S = () => pick(['ok', 'x', 'a'.repeat(pick([1, 499, 500, 501, 2000])), '<img src=x onerror=1>', '"\'><script>', '\u0000', 'javascript:1']);
    const kids = () => Array.from({ length: Math.floor(rnd() * 4) }, () => d < 9 ? genNode(d + 1) : gen(9));
    const maybe = (k, v) => { if (rnd() < 0.7) o[k] = v; };
    switch (t) {
      case 'text': maybe('text', S()); maybe('tone', pick(['ok','bad','nope',5])); maybe('mono', pick([true, 1, 'y'])); break;
      case 'badge': maybe('text', S()); maybe('tone', pick(['ok','warn','nope'])); maybe('title', S()); break;
      case 'status': maybe('state', pick(['ok','bad','pwned',1])); maybe('text', S()); break;
      case 'stat': maybe('value', pick([1, 'x', S(), null])); maybe('unit', S()); maybe('label', S()); break;
      case 'gauge': maybe('pct', pick([0, 50, 100, 150, -3, NaN, '7', null, Infinity])); maybe('label', S()); maybe('text', S()); break;
      case 'kv': maybe('rows', Array.from({ length: Math.floor(rnd() * 4) }, () => pick([[S(), S()], [S()], [S(), gen(5)], 'x', [S(), genNode(5)]]))); break;
      case 'link': maybe('text', S()); maybe('href', pick(['https://ok.example/x', '/local', 'javascript:1', '//evil', 'data:x', S(), 5])); break;
      case 'button': maybe('label', S()); maybe('action', pick(['a', 'a/b', '../x', '/x', 'a?b', S(), 5])); maybe('method', pick(['GET','POST','TRACE',5]));
        maybe('style', pick(['ok','evil'])); maybe('caps', pick([['admin'], 'admin', [5], []]));
        maybe('fields', Array.from({ length: Math.floor(rnd() * 4) }, () => pick([{ name: 'a', type: 'select', options: [S()] }, { name: 'a b' }, { name: pick(['ok_1', '1x', S()]), type: pick(['text','evil']) }, 5]))); break;
      case 'empty': maybe('title', S()); maybe('text', S()); break;
      case 'stack': case 'columns': case 'section': maybe('children', pick([kids(), kids(), 'x', null])); maybe('title', S()); maybe('dir', pick(['row','col','x'])); break;
      case 'table': maybe('columns', Array.from({ length: Math.floor(rnd() * 4) + (rnd() < 0.1 ? 12 : 0) }, () => pick([{ key: 'a' }, { key: 'b', label: S(), align: 'right' }, { key: S() }, 5])));
        maybe('rows', Array.from({ length: Math.floor(rnd() * 4) }, () => pick([{ a: S(), b: genNode(7) }, { a: gen(6) }, 'x', { a: 1, b: 2, _id: S() }])));
        maybe('row_actions', pick([[genNode(7)], [{ type: 'button', label: 'x', action: 'a' }], 'x', []])); break;
    }
    return o;
  }
  const S0 = () => pick(['ok', '<b>', 'a'.repeat(130), '', '__proto__', 'a b']);
  const count = { n: 0 };
  for (let i = 0; i < 6000; i++) {
    fresh(); inst.callOk = () => true; inst.renderOk = () => true; inst.toastOk = () => true;
    const r0 = rnd();
    if (r0 < 0.05) {
      inst.openOk = () => true;
      const u = pick(['https://example.local/', 'javascript:alert(1)', 'data:x', '//example.local', 'https://u:p@example.local', ' https://example.local',
                      'http:example.local', 'https:\\\\example.local', 'file:///x', gen(2), pick(words) + pick(words)]);
      const e = send({ op: 'open', id: 1, url: u });
      if (e) { fail('fuzz: open threw', e.message); break; }
      for (const o of rec.opened) if (!/^https?:\/\/[^/@]+\//.test(o[0]) || o[0].length > 2000 || o[2] !== 'noopener,noreferrer') fail('fuzz: bad URL reached window.open', JSON.stringify(o));
      continue;
    }
    const m = r0 < 0.3 ? gen(0) : r0 < 0.75 ? { op: 'render', slot: pick(spec.renders), key: pick(['h1', 'host:2', S0(), null]), node: pick([genNode(0), genNode(0), genNode(0), gen(0), null]) } : { op: pick(['render','call','read','toast','resize','pong','ready','x']), id: gen(3), slot: pick(spec.renders.concat(words)),
      key: gen(3), node: gen(0), route: gen(3), method: gen(3), name: gen(3), h: gen(3), msg: gen(3), body: gen(2) };
    const e = send(m);
    if (e) { fail('fuzz: onMessage threw', e.message + ' :: ' + JSON.stringify(m).slice(0, 160)); break; }
    for (const v of rec.virt) { checkClean(v[5], 0, count, JSON.stringify(m).slice(0, 120)); if (typeof v[4] === 'string' && v[4].length > 128) fail('fuzz: key too long', v[4].length); }
    if (failures.length > 20) break;
  }
  if (count.n === 0) fail('fuzz produced no accepted nodes — the generator is broken', '');
  if (rec.nav.length) fail('the dashboard was navigated', JSON.stringify(rec.nav));
  console.log(JSON.stringify({ failures, fuzzed_nodes: count.n }));
})();
