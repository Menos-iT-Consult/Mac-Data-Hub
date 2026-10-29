(function () {
  var overlay = document.getElementById('ref-overlay');
  if (!overlay) return;
  var code = overlay.dataset.code;
  var link = overlay.dataset.appUrl + '/register?ref=' + encodeURIComponent(code);

  function close() { overlay.classList.add('ref-hide'); document.body.style.overflow = ''; }
  document.body.style.overflow = 'hidden';

  document.getElementById('ref-close').addEventListener('click', close);
  document.getElementById('ref-later').addEventListener('click', close);
  overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });

  document.getElementById('ref-copy').addEventListener('click', function () {
    var label = document.getElementById('ref-copy-text');
    function done() { label.textContent = 'Copied!'; setTimeout(function () { label.textContent = 'Copy'; }, 1800); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(code).then(done, done);
    } else {
      var t = document.createElement('textarea'); t.value = code; document.body.appendChild(t);
      t.select(); try { document.execCommand('copy'); } catch (e) {} t.remove(); done();
    }
  });

  document.getElementById('ref-share').addEventListener('click', function () {
    var msg = 'Buy cheap data on Mac Data Hub! Sign up with my code ' + code + ' or use this link: ' + link;
    window.open('https://wa.me/?text=' + encodeURIComponent(msg), '_blank', 'noopener');
  });
})();
