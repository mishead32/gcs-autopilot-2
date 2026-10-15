/* Don't lose a half-built flow.
 *
 * Building an FMS flow is twenty minutes of typing: a start form, then six or
 * eight steps each with a title, a doer, a TAT, routing and an anchor. Lose it
 * to a stray refresh, a phone call that puts the laptop to sleep, or a session
 * that times out, and the whole lot has to be typed again. People stopped
 * building flows because of it.
 *
 * So the form saves itself as you type — into THIS BROWSER, not the server.
 * That is deliberate: a draft is personal and half-finished, it should not
 * cost a database write every few seconds, and it must survive the very thing
 * that loses it, which is the page going away before anything was submitted.
 *
 * On coming back, nothing is restored behind your back. A bar appears saying
 * what was found and when, and you choose. Restoring the wrong draft over
 * something you meant to start fresh would be a worse bug than the one this
 * fixes.
 */
(function () {
  var form = document.querySelector('form[data-draft]');
  if (!form) return;

  var KEY = 'gcsa:flowdraft:' + form.dataset.draft;
  var SAVE_AFTER = 600;          // ms of quiet before writing
  var KEEP_DAYS = 7;

  // Storage can be switched off, full, or refused in a private window. None
  // of that may break the page: the form must still work exactly as it did
  // before this file existed.
  function store(fn, fallback) {
    try { return fn(window.localStorage); } catch (e) { return fallback; }
  }

  /* ---------------------------------------------------------- reading ---
   * Every control with a name, in document order, as a flat list. Order is
   * what makes this work: the server already reads these as parallel lists,
   * and the restore rebuilds the same number of rows before filling them, so
   * position carries the meaning without inventing an id scheme.
   */
  function snapshot() {
    var out = [];
    form.querySelectorAll('input[name], select[name], textarea[name]').forEach(
      function (el) {
        if (el.type === 'file' || el.type === 'submit' || el.type === 'button') return;
        if (el.type === 'checkbox' || el.type === 'radio') {
          out.push([el.name, el.checked ? el.value : '']);
        } else {
          out.push([el.name, el.value]);
        }
      });
    return out;
  }

  function countOf(snap, name) {
    var n = 0;
    snap.forEach(function (p) { if (p[0] === name) n++; });
    return n;
  }

  function isEmpty(snap) {
    // A page nobody typed into is not a draft worth offering back.
    return !snap.some(function (p) {
      var v = (p[1] || '').trim();
      if (!v) return false;
      // Selects arrive with a default already chosen; those are not typing.
      return p[0] === 'name' || p[0] === 'description'
          || p[0] === 'sf_label' || p[0] === 'sf_options'
          || p[0] === 'step_title' || p[0] === 'step_instructions'
          || p[0] === 'step_fields';
    });
  }

  /* ---------------------------------------------------------- writing --- */
  var timer = null;
  function save() {
    var snap = snapshot();
    if (isEmpty(snap)) { store(function (s) { s.removeItem(KEY); }); return; }
    store(function (s) {
      s.setItem(KEY, JSON.stringify({ at: Date.now(), fields: snap }));
    });
  }
  function saveSoon() {
    clearTimeout(timer);
    timer = setTimeout(save, SAVE_AFTER);
  }

  form.addEventListener('input', saveSoon);
  form.addEventListener('change', saveSoon);
  // Adding or removing a step is a click, with no input event behind it.
  form.addEventListener('click', function () { setTimeout(saveSoon, 0); });

  // Submitting does NOT throw the draft away.
  //
  // It used to, and that was the whole feature undone: the server refuses a
  // flow whose routing points at a step that does not exist, and the refusal
  // arrives as an error page — by which time the draft had already been
  // deleted and twenty minutes of work with it. Exactly the loss this file
  // was written to prevent, caused by this file.
  //
  // So the draft is only cleared when the save is KNOWN to have gone
  // through: the server redirects to the flow with ?built= on it, and the
  // flow's own page clears it there. A refused save leaves the draft alone,
  // and going back to the builder offers it.
  form.addEventListener('submit', function () {
    clearTimeout(timer);
    save();
  });

  /* -------------------------------------------------------- restoring --- */
  function restore(snap) {
    // Build the rows first. Both builders expose a maker that appends one
    // and wires it; without this the values would have nowhere to go.
    var wantFields = countOf(snap, 'sf_label');
    var wantSteps = countOf(snap, 'step_title');

    var sf = document.getElementById('sfields');
    if (sf && typeof addField === 'function') {
      while (sf.querySelectorAll('.sfrow').length > wantFields) {
        sf.querySelector('.sfrow').remove();
      }
      while (sf.querySelectorAll('.sfrow').length < wantFields) addField();
    }
    var st = document.getElementById('steps');
    if (st && typeof addStep === 'function') {
      while (st.querySelectorAll('.stepcard').length > wantSteps) {
        st.querySelector('.stepcard').remove();
      }
      while (st.querySelectorAll('.stepcard').length < wantSteps) addStep();
    }
    if (typeof renumber === 'function') renumber();
    if (typeof refreshDateFields === 'function') refreshDateFields();

    // Now the values, in the same order they were read in.
    var els = [];
    form.querySelectorAll('input[name], select[name], textarea[name]').forEach(
      function (el) {
        if (el.type === 'file' || el.type === 'submit' || el.type === 'button') return;
        els.push(el);
      });

    snap.forEach(function (pair, i) {
      var el = els[i];
      if (!el || el.name !== pair[0]) return;      // shape moved on; skip it
      if (el.type === 'checkbox' || el.type === 'radio') {
        el.checked = pair[1] === el.value;
      } else {
        el.value = pair[1];
      }
      el.dispatchEvent(new Event('change', { bubbles: true }));
    });

    // The question labels only became known as they were filled in, so the
    // anchor list is rebuilt once more and the anchors re-applied — their
    // options did not exist on the first pass.
    if (typeof refreshDateFields === 'function') {
      refreshDateFields();
      snap.forEach(function (pair, i) {
        if (pair[0] === 'step_due_from' && els[i]) els[i].value = pair[1];
      });
    }
  }

  function ago(ms) {
    var m = Math.round((Date.now() - ms) / 60000);
    if (m < 1) return 'a moment ago';
    if (m < 60) return m + ' minute' + (m === 1 ? '' : 's') + ' ago';
    var h = Math.round(m / 60);
    if (h < 24) return h + ' hour' + (h === 1 ? '' : 's') + ' ago';
    return Math.round(h / 24) + ' day(s) ago';
  }

  function offer(draft) {
    var bar = document.createElement('div');
    bar.className = 'draftbar';
    bar.innerHTML =
      '<span class="t">You were building this ' + ago(draft.at) +
      ' and did not finish. Bring it back?</span>' +
      '<button type="button" class="btn sm" data-yes>Restore it</button>' +
      '<button type="button" class="btn ghost sm" data-no>Start fresh</button>';
    form.parentNode.insertBefore(bar, form);

    bar.querySelector('[data-yes]').addEventListener('click', function () {
      restore(draft.fields);
      bar.querySelector('.t').textContent = 'Brought back. Carry on where you left off.';
      bar.querySelectorAll('button').forEach(function (b) { b.remove(); });
      setTimeout(function () { bar.remove(); }, 2600);
    });
    bar.querySelector('[data-no]').addEventListener('click', function () {
      store(function (s) { s.removeItem(KEY); });
      bar.remove();
    });
  }

  var raw = store(function (s) { return s.getItem(KEY); }, null);
  if (!raw) return;
  var draft;
  try { draft = JSON.parse(raw); } catch (e) { draft = null; }
  if (!draft || !draft.fields || !draft.fields.length) {
    store(function (s) { s.removeItem(KEY); });
    return;
  }
  // An old draft is clutter, not help.
  if (Date.now() - (draft.at || 0) > KEEP_DAYS * 86400000) {
    store(function (s) { s.removeItem(KEY); });
    return;
  }
  offer(draft);
})();
