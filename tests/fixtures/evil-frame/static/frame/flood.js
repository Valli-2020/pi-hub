// Hammer the bridge: calls, renders, toasts, malformed messages.
(function () {
  var ok = 0, limited = 0, other = 0;
  for (var i = 0; i < 300; i++) ph.call('samples').then(function () { ok++; }, function (e) { if (e && e.status === 429) limited++; else other++; });
  for (var j = 0; j < 200; j++) { ph.render('hosts.card.badges', 'k' + j, { type: 'badge', text: 'x' }); ph.toast('spam ' + j); }
  // The report itself may be refused (the frame is being stopped) — that is fine, the
  // test reads the server-side hit counter as well.
  setTimeout(function () { ph.call('report', { method: 'POST', body: { frame: 'flood', results: { ok: ok, limited: limited, other: other } } }).catch(function () {}); }, 1500);
})();
