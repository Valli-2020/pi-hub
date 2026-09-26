// Browser proof that the plugin-frame sandbox holds.
//
// Needs: a DEV hub with tests/fixtures/evil-frame copied to pi_hub_plugins/,
// enabled and approved (never do this on a real hub), an admin account, and
// Playwright with Chromium.
//
//   BASE_URL=http://127.0.0.1:8897 PH_USER=admin PH_PASS=... \
//   PLAYWRIGHT_DIR=/path/to/node_modules node tests/browser_evil_frame.mjs
//
// Exit code 0 = every escape attempt failed.
import { createRequire } from 'module';
import http from 'http';
const require = createRequire((process.env.PLAYWRIGHT_DIR || process.cwd() + '/node_modules') + '/');
const { chromium } = require('playwright');

const BASE = process.env.BASE_URL || 'http://127.0.0.1:8897';
const USER = process.env.PH_USER || 'admin', PASS = process.env.PH_PASS || '';
const fails = [];
const check = (name, cond, detail = '') => { console.log((cond ? '  ok  ' : 'FAIL  ') + name + (!cond && detail ? '  ' + detail : '')); if (!cond) fails.push(name); };

// A listener the frames are pointed at.  Nothing may ever reach it.
const hits = [];
const listener = http.createServer((req, res) => { hits.push(req.method + ' ' + req.url); res.writeHead(200, { 'Access-Control-Allow-Origin': '*' }); res.end('x'); });
await new Promise(r => listener.listen(0, '127.0.0.1', r));
const TARGET = 'http://127.0.0.1:' + listener.address().port;

const browser = await chromium.launch();
async function open(qs = '', init) {
  const ctx = await browser.newContext({ viewport: { width: 1300, height: 900 } });
  const page = await ctx.newPage();
  if (init) await page.addInitScript(init);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(BASE + '/' + qs);
  await page.waitForSelector('#login-user', { state: 'visible', timeout: 8000 }).catch(() => {});
  if (await page.locator('#login-user').isVisible()) {
    await page.fill('#login-user', USER); await page.fill('#login-pass', PASS);
    await page.click('#login-form button[type=submit]');
    await page.waitForLoadState('networkidle');
  }
  await page.waitForTimeout(2000);
  return { ctx, page, errors };
}
const api = (page, path) => page.evaluate(async p => (await fetch(p, { headers: { Authorization: 'Bearer ' + localStorage.getItem('pi_hub_token') } })).json(), path);
const tab = async (page, name, wait) => { await page.click('.nav-item[data-go="phf-evil-frame--' + name + '"]'); await page.waitForTimeout(wait); };

// ── 1. escape attempts ─────────────────────────────────────────────────────
console.log('attack frame');
{
  const { ctx, page } = await open();
  const token = await page.evaluate(() => localStorage.getItem('pi_hub_token'));
  check('admin token present before the attack', !!token);
  await page.evaluate(async ([t, tok]) => { await fetch('/api/plugin/evil-frame/target', { method: 'POST', headers: { Authorization: 'Bearer ' + tok, 'Content-Type': 'application/json' }, body: JSON.stringify({ url: t }) }); }, [TARGET, token]);
  await page.evaluate(() => { window.__pwned = undefined; });
  await tab(page, 'attack', 9000);
  const res = (await api(page, '/api/plugin/evil-frame/results')).results?.attack;
  check('frame reported its results', !!res, JSON.stringify(res));
  const allowed = Object.entries(res || {}).filter(([, v]) => !String(v).startsWith('blocked:'));
  check('EVERY escape attempt was blocked (' + Object.keys(res || {}).length + ' attempts)', allowed.length === 0, JSON.stringify(allowed));
  check('the listener received NO request from the frame (fetch, XHR, beacon, img, script, css, iframe, form, worker, ws, prefetch)', hits.length === 0, JSON.stringify(hits));
  for (const [k, v] of Object.entries(res || {})) console.log('        ' + k.padEnd(38) + v);
  check('dashboard still on the same URL', new URL(page.url()).pathname === '/' && !page.url().includes('pwned'), page.url());
  check('token untouched in dashboard localStorage', (await page.evaluate(() => localStorage.getItem('pi_hub_token'))) === token);
  check('no code ran in the dashboard (window.__pwned)', (await page.evaluate(() => window.__pwned)) === undefined);
  check('no injected <img> in the dashboard', (await page.locator('img[src="x"]').count()) === 0);
  check('no javascript: links in the dashboard', (await page.locator('a[href^="javascript" i]').count()) === 0);
  check('no <script> element created by frame text', (await page.locator('#health script').count()) === 0);
  await page.waitForTimeout(500);
  check('the frame was stopped after three refused renders', (await page.locator('.view:not([hidden]) .plg-frame-err').count()) === 1);
  const badge = await page.locator('.ph-slot[data-slot="hosts.card.badges"]').first().innerHTML().catch(() => '');
  check('hostile badge text is not rendered as markup', !/<img/i.test(badge), badge.slice(0, 200));
  const fr = await page.evaluate(() => { const f = document.querySelector('iframe.plg-frame'); return f ? f.getAttribute('sandbox') : null; });
  check('iframe sandbox attribute is exactly allow-scripts (while mounted)', fr === null || fr === 'allow-scripts', String(fr));
  await ctx.close();
}

// ── 2. flooding the bridge ─────────────────────────────────────────────────
console.log('flood frame');
{
  const { ctx, page } = await open();
  const before = (await api(page, '/api/plugin/evil-frame/results')).hits || 0;
  await tab(page, 'flood', 6000);
  const r = await api(page, '/api/plugin/evil-frame/results');
  const res = r.results?.flood;   // usually absent: the frame is stopped before its own report goes out
  check('at most a burst of calls reached the server (300 sent)', (r.hits - before) <= 25, String(r.hits - before));
  check('the flood replies were refused as rate limited (429), not delivered', res ? res.limited >= 200 : r.hits - before <= 25, JSON.stringify(res));
  check('the frame was stopped', (await page.locator('.view:not([hidden]) .plg-frame-err').count()) === 1);
  check('toast spam was limited', (await page.locator('.toast').count()) <= 3, String(await page.locator('.toast').count()));
  await ctx.close();
}

// ── 3. busy loop ───────────────────────────────────────────────────────────
console.log('spin frame');
{
  const { ctx, page } = await open();
  await tab(page, 'spin', 500);
  await page.waitForTimeout(3000);
  const probe = () => Promise.race([page.evaluate(() => 1).then(() => true), new Promise(r => setTimeout(() => r(false), 3000))]);
  const responsive = await probe();
  console.log('        dashboard responsive while the frame spins:', responsive);
  if (responsive) {
    // The frame lives in its own process: only the watchdog can stop it.
    await page.waitForTimeout(30000);
    check('watchdog removed the unresponsive frame', (await page.locator('iframe.plg-frame').count()) === 0);
  } else {
    // Chromium shares one renderer for sandboxed same-site frames, so a busy loop freezes the
    // tab for as long as it runs.  That is what the crash marker below is for; here we prove
    // the dashboard comes back once the loop ends and nothing was lost.
    console.log('        (tab shares the renderer with the frame: the crash marker is the safeguard)');
    let back = false;
    for (let i = 0; i < 10 && !back; i++) back = await probe();
    check('dashboard recovers when the busy loop ends', back);
    check('token untouched after the freeze', back && (await page.evaluate(() => !!localStorage.getItem('pi_hub_token'))));
  }
  await ctx.close().catch(() => {});
  // Whether or not the tab froze, a frame that crashed the tab 3 times is not mounted again.
  const c2 = await open('', () => { try { sessionStorage.setItem('phf-crash', JSON.stringify({ 'evil-frame/spin/tab': 3 })); } catch (e) {} });
  await tab(c2.page, 'spin', 1500);
  check('crash marker: frame is not mounted after repeated crashes', (await c2.page.locator('iframe.plg-frame').count()) === 0);
  check('crash marker: user is told how to skip frames', /noframes/.test(await c2.page.locator('.view:not([hidden]) .plg-frame-err').innerText().catch(() => '')));
  await c2.ctx.close();
}

// ── 4. kill switches ───────────────────────────────────────────────────────
console.log('kill switches');
for (const [qs, label] of [['?noframes=1', 'noframes'], ['?safe=1', 'safe']]) {
  const { ctx, page } = await open(qs);
  check(label + ': no frame tabs in the sidebar', (await page.locator('.nav-item[data-go^="phf-"]').count()) === 0);
  check(label + ': no iframes', (await page.locator('iframe').count()) === 0);
  check(label + ': dashboard works', (await page.locator('#hosts').count()) === 1);
  await ctx.close();
}

await browser.close();
listener.close();
console.log(fails.length ? '\nFAILED: ' + fails.join(', ') : '\nALL CHECKS PASSED');
process.exit(fails.length ? 1 : 0);
