/* Saying what a date box actually holds, in dd/mm/yyyy.
 *
 * Every date this software WRITES is dd/mm/yyyy — the pages, the reports,
 * the emails, the Excel downloads. The one thing it cannot write is the
 * inside of the browser's own date box: `<input type="date">` is drawn by
 * the browser in the BROWSER's language, not the page's, and nothing a
 * server sends can change that. A laptop set to English (United States)
 * shows 11/08/2026 in that box while the rest of the page says 08/11/2026,
 * and the two readings are both real dates, which is exactly the confusion
 * the rule exists to prevent.
 *
 * So: when the browser is not already day-first, every date box gets a
 * small grey dd/mm/yyyy under it saying what is really selected. When the
 * browser IS day-first — anyone whose Chrome is set to English (India) or
 * (United Kingdom) — nothing is added, because a second copy of a date
 * already shown correctly is clutter.
 *
 * The real fix is one setting on the machine, and the hint says so once.
 */
(function () {
  'use strict';

  function dayFirst() {
    // What order does this browser put a date in? Asked of Intl rather
    // than guessed from the language tag, because "en-IN" and "en-GB"
    // and "hi" are all day-first and listing them by hand is a list that
    // goes out of date.
    try {
      var parts = new Intl.DateTimeFormat(navigator.language || undefined)
        .formatToParts(new Date(2026, 10, 8));
      for (var i = 0; i < parts.length; i++) {
        if (parts[i].type === 'day') return true;
        if (parts[i].type === 'month') return false;
      }
    } catch (e) {
      return false;          // no Intl: say it, rather than assume
    }
    return false;
  }

  if (dayFirst()) return;

  function show(value) {
    // The value of a date input is always ISO, whatever the box displays.
    var m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/.exec(value || '');
    if (!m) return '';
    var out = m[3] + '/' + m[2] + '/' + m[1];
    if (m[4]) {
      var h = Number(m[4]);
      var ampm = h < 12 ? 'AM' : 'PM';
      var h12 = h % 12 || 12;
      out += ' ' + (h12 < 10 ? '0' : '') + h12 + ':' + m[5] + ' ' + ampm;
    }
    return out;
  }

  // Short enough to sit on one line inside a filter bar. The box itself
  // is drawn by the browser in the browser's order and no page can
  // change that, so the note says which order it is in and the tooltip
  // says where the real fix lives. A paragraph here reflowed the whole
  // filter row and pushed the next field off its line.
  var ADVICE = '\u26a0 this browser\u2019s date boxes are mm/dd';
  var WHY = 'The date box above is drawn by your browser in your ' +
    'browser\u2019s language, not by this site. Everything else here is ' +
    'dd/mm/yyyy. To make the box match, set the browser\u2019s language ' +
    'to English (India).';
  var advised = false;

  function wire(box) {
    if (box.dataset.dfWired === '1') return;
    box.dataset.dfWired = '1';
    var note = document.createElement('div');
    note.className = 'm datefmt';
    box.insertAdjacentElement('afterend', note);

    function draw() {
      // What is really in the box, in the order the rest of the page
      // uses. An EMPTY box shows the browser's own "mm/dd/yyyy"
      // placeholder, which is drawn by the browser and cannot be
      // changed from here — which is what the advice below is for.
      note.textContent = show(box.value);
    }
    box.addEventListener('change', draw);
    box.addEventListener('input', draw);
    draw();

    // Once per page, under the first date box: the one thing that
    // actually fixes the box itself. Under every box it would be a
    // paragraph of advice sitting in the middle of a filter bar.
    if (!advised) {
      advised = true;
      var tip = document.createElement('div');
      tip.className = 'm datefmt advice';
      tip.textContent = ADVICE;
      tip.title = WHY;
      note.insertAdjacentElement('afterend', tip);
    }
  }

  function sweep() {
    document.querySelectorAll(
      'input[type="date"], input[type="datetime-local"]').forEach(wire);
  }

  sweep();
  // Step cards and filter rows are added to the page long after this runs.
  if (window.MutationObserver) {
    new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        if (muts[i].addedNodes.length) { sweep(); return; }
      }
    }).observe(document.body, { childList: true, subtree: true });
  }
})();
