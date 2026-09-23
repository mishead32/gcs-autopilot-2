/* Live benchmark total: keeps the three inputs adding up to 100 and blocks
   submission until they do, so a bad split can never reach the server. */
(function () {
  document.querySelectorAll('[data-bm]').forEach(function (box) {
    var inputs = box.querySelectorAll('input[type=number]');
    var totBox = box.querySelector('.tot-box');
    var totVal = box.querySelector('.tv');
    var msg = box.parentNode.querySelector('.bm-msg');
    var form = box.closest('form');
    if (!inputs.length || !form) return;

    function sum() {
      var t = 0;
      inputs.forEach(function (i) { t += parseInt(i.value, 10) || 0; });
      return t;
    }

    function check() {
      var t = sum(), ok = t === 100;
      totVal.textContent = t;
      totBox.classList.toggle('ok', ok);
      totBox.classList.toggle('bad', !ok);
      if (msg) {
        msg.hidden = ok;
        if (!ok) {
          msg.textContent = t > 100
            ? 'That is ' + (t - 100) + '% over. The three must total exactly 100%.'
            : 'That is ' + (100 - t) + '% short. The three must total exactly 100%.';
        }
      }
      return ok;
    }

    inputs.forEach(function (i) {
      i.addEventListener('input', check);
      i.addEventListener('change', check);
    });

    form.addEventListener('submit', function (e) {
      if (!check()) {
        e.preventDefault();
        box.scrollIntoView({ behavior: 'smooth', block: 'center' });
        inputs[0].focus();
      }
    });

    check();
  });
})();
