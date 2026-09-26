// Freeze the renderer after a moment (10 s, so the test can observe the recovery).
setTimeout(function () { var t = Date.now(); while (Date.now() - t < 10000) { /* spin */ } }, 1500);
