// Runs sandboxed (opaque origin).  No network, no storage: everything goes
// through the `ph` bridge.  Text from the hub (host names, event lines) is
// only ever put in with textContent — never innerHTML.
(function () {
  'use strict';
  var root = document.body;
  var timer = 0, busy = false;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function tone(ms) { return ms == null ? 'bad' : ms > 250 ? 'bad' : ms > 80 ? 'warn' : 'ok'; }

  function spark(canvas, pts) {
    var dpr = window.devicePixelRatio || 1, w = canvas.clientWidth || 200, h = 28;
    canvas.width = w * dpr; canvas.height = h * dpr;
    var g = canvas.getContext('2d');
    g.scale(dpr, dpr); g.clearRect(0, 0, w, h);
    var vals = pts.filter(function (v) { return v != null; });
    var max = Math.max.apply(null, vals.concat([20])), step = w / Math.max(pts.length - 1, 1);
    var vars = ph.theme.vars;
    g.lineWidth = 1.5; g.strokeStyle = vars['--accent'] || '#b45309'; g.beginPath();
    var open = false;
    pts.forEach(function (v, i) {
      if (v == null) { open = false; return; }
      var x = i * step, y = h - 3 - (v / max) * (h - 6);
      if (open) g.lineTo(x, y); else { g.moveTo(x, y); open = true; }
    });
    g.stroke();
    g.fillStyle = vars['--bad'] || '#b91c1c';
    pts.forEach(function (v, i) { if (v == null) g.fillRect(i * step - 1, h - 4, 2, 3); });
  }

  function draw(data) {
    var box = document.getElementById('rows');
    box.textContent = '';
    if (!data.hosts.length) { box.appendChild(el('div', 'empty', 'Waiting for the first samples…')); return; }
    data.hosts.forEach(function (hst) {
      var last = hst.points.length ? hst.points[hst.points.length - 1] : null;
      var row = el('div', 'row');
      row.appendChild(el('span', 'name', hst.name));
      var c = el('canvas'); row.appendChild(c);
      row.appendChild(el('span', 'ms' + (last == null ? ' bad' : ''), last == null ? 'down' : last + ' ms'));
      box.appendChild(row);
      spark(c, hst.points);
      // A latency badge in the host card itself, rendered by the dashboard.
      ph.render('hosts.card.badges', hst.id, { type: 'badge', text: last == null ? 'down' : last + ' ms', tone: tone(last) });
    });
  }

  function drawEvents(data) {
    var log = document.getElementById('log');
    if (!log) return;
    log.textContent = '';
    if (!data.events.length) { log.appendChild(el('div', 'empty', 'No host state changes yet.')); return; }
    data.events.slice().reverse().forEach(function (e) {
      log.appendChild(el('div', null, new Date(e.ts * 1000).toLocaleTimeString() + '  ' + e.host + '  ' + e.text));
    });
  }

  function poll() {
    if (busy || !ph.visible) return;
    busy = true;
    ph.call('samples').then(draw)
      .then(function () { return document.getElementById('log') ? ph.call('events').then(drawEvents) : null; })
      .catch(function () { /* transient */ })
      .then(function () { busy = false; });
  }

  root.appendChild(el('h4', null, ph.surface === 'tab' ? 'Latency (last 5 minutes)' : 'Live pulse'));
  root.appendChild(el('div')).id = 'rows';
  if (ph.surface === 'tab') {
    root.appendChild(el('h4', null, 'Host state changes')).style.marginTop = '16px';
    root.appendChild(el('div', 'log')).id = 'log';
  }
  ph.on('visible', function (v) { if (v) poll(); });
  ph.on('refresh', poll);
  ph.on('theme', function () { poll(); });
  poll();
  timer = setInterval(poll, 5000);
})();
