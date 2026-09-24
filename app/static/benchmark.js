/* Live benchmark total.
 *
 * The three shares no longer have to reach 100. Part of an EM score is
 * judged by hand, so 40/10/10 is a deliberate statement: the software
 * accounts for 60 points and a person decides the other 40. This used to
 * block the form until the three hit exactly 100, which made that split
 * impossible to enter.
 *
 * So it now explains rather than blocks. The one thing it still refuses is
 * a total above 100, because the three together are the most the software
 * can deduct and it cannot deduct more than the whole score.
 */
(function () {
  document.querySelectorAll('[data-bm]').forEach(function (box) {
    var inputs = box.querySelectorAll('input[type=number]');
    var totBox = box.querySelector('.tot-box');
    var totVal = box.querySelector('.tv');
    var msg = box.parentNode.querySelector('.bm-msg');
    var note = box.parentNode.querySelector('.bm-note');
    var form = box.closest('form');
    if (!inputs.length || !form) return;

    function sum() {
      var t = 0;
      inputs.forEach(function (i) { t += parseInt(i.value, 10) || 0; });
      return t;
    }

    function check() {
      var t = sum(), over = t > 100;
      totVal.textContent = t;
      totBox.classList.toggle('ok', !over);
      totBox.classList.toggle('bad', over);

      if (msg) {
        msg.hidden = !over;
        if (over) {
          msg.textContent = 'That is ' + (t - 100) + '% over. The three together '
            + 'are the most the software can take off, so they cannot add up to '
            + 'more than 100.';
        }
      }

      // Not a warning — a plain statement of what this split means. Silent
      // while the total is impossible, so it cannot claim "all 100 points"
      // underneath an error saying the total is 180.
      if (note) {
        if (over) {
          note.textContent = '';
        } else if (t === 0) {
          note.textContent = 'At 0%, the software will not score this person at '
            + 'all — every point is decided by hand.';
        } else if (t < 100) {
          note.textContent = 'The software scores ' + t + ' of the 100 points; the '
            + 'other ' + (100 - t) + ' are yours to set by hand.';
        } else {
          note.textContent = 'The software scores all 100 points.';
        }
      }
      return !over;
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
