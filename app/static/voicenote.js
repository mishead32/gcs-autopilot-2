/* Dictating a delegated task.
 *
 * Record in the page, or choose a recording already on the machine; send it
 * once; put what comes back into the boxes below. Everything this writes is
 * a DRAFT — the person reads it and presses the button, and nothing here
 * submits anything.
 *
 * Two rules it keeps to:
 *
 *   It never overwrites typing. If the title or the details already have
 *   something in them, the transcript goes underneath rather than over the
 *   top. Somebody who typed two lines and then reached for the microphone
 *   should not lose the two lines.
 *
 *   It never picks the person or the date. Both are offered as a chip to
 *   tap. A task silently assigned to the wrong Rohit, found three days
 *   later when nothing has been done, costs far more than one click.
 *
 * If this file fails to load, or the browser has no microphone API, the
 * card simply says so and the form underneath works exactly as it always
 * did. It is an accelerator sitting on top of a form, not part of it.
 */
(function () {
  'use strict';

  var card = document.getElementById('vn');
  if (!card) return;

  var recBtn = document.getElementById('vnRec');
  var stopBtn = document.getElementById('vnStop');
  var fileIn = document.getElementById('vnFile');
  var langIn = document.getElementById('vnLang');
  var saying = document.getElementById('vnSay');
  var out = document.getElementById('vnOut');
  var player = document.getElementById('vnPlay');
  var dropBtn = document.getElementById('vnDrop');
  var whoBox = document.getElementById('vnWho');
  var whenBox = document.getElementById('vnWhen');
  var howBox = document.getElementById('vnHow');
  var doneNote = document.getElementById('vnDone');
  var errBox = document.getElementById('vnErr');
  var token = document.getElementById('vnToken');

  var titleIn = document.querySelector('input[name="title"]');
  var detailsIn = document.querySelector('textarea[name="details"]');
  var dueIn = document.querySelector('input[name="due_at"]');
  var prioIn = document.querySelector('select[name="priority"]');
  var picker = document.querySelector('.doerpick .dp-src');

  var maxMb = parseFloat(card.dataset.max || '12');
  var rec = null, chunks = [], started = 0, ticker = null, blobUrl = null;

  function say(text) { saying.textContent = text || ''; }

  function fail(text) {
    errBox.textContent = text;
    errBox.hidden = !text;
  }

  function busy(on) {
    recBtn.disabled = on;
    fileIn.disabled = on;
  }

  /* --------------------------------------------------------- recording -- */

  function canRecord() {
    return !!(navigator.mediaDevices &&
              navigator.mediaDevices.getUserMedia &&
              window.MediaRecorder);
  }

  if (!canRecord()) {
    // Older Safari, and any page not served over HTTPS. Choosing a file
    // still works, so only the Record button goes away.
    recBtn.hidden = true;
    say('This browser will not record here — choose a recording instead.');
  }

  function pickType() {
    // Chrome and Firefox write webm/opus; Safari writes mp4/aac. Both are
    // containers the transcription service accepts, so whichever the
    // browser can actually produce is the one used.
    var want = ['audio/webm', 'audio/mp4', 'audio/ogg'];
    for (var i = 0; i < want.length; i++) {
      if (window.MediaRecorder.isTypeSupported &&
          window.MediaRecorder.isTypeSupported(want[i])) return want[i];
    }
    return '';
  }

  recBtn.addEventListener('click', function () {
    fail('');
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      var type = pickType();
      rec = new window.MediaRecorder(stream, type ? { mimeType: type } : undefined);
      chunks = [];
      rec.ondataavailable = function (e) { if (e.data.size) chunks.push(e.data); };
      rec.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        var blob = new Blob(chunks, { type: rec.mimeType || type || 'audio/webm' });
        var secs = Math.round((Date.now() - started) / 1000);
        send(blob, 'voice-note', secs);
      };
      started = Date.now();
      rec.start();
      recBtn.hidden = true;
      stopBtn.hidden = false;
      say('Listening…');
      ticker = setInterval(function () {
        var s = Math.round((Date.now() - started) / 1000);
        say('Listening… ' + Math.floor(s / 60) + ':' +
            String(s % 60).padStart(2, '0'));
        // A hard stop. Five minutes of plain WAV is about ten megabytes,
        // which is the most the server takes — and a dictated task that
        // runs past five minutes is not a task, it is a meeting.
        if (s >= 300) stopBtn.click();
      }, 1000);
    }).catch(function () {
      fail('The microphone was not allowed. Allow it for this site in the ' +
           'browser, or choose a recording instead.');
    });
  });

  stopBtn.addEventListener('click', function () {
    if (ticker) { clearInterval(ticker); ticker = null; }
    stopBtn.hidden = true;
    recBtn.hidden = false;
    if (rec && rec.state !== 'inactive') rec.stop();
  });

  fileIn.addEventListener('change', function () {
    var f = fileIn.files && fileIn.files[0];
    if (!f) return;
    fail('');
    send(f, f.name, 0);
    fileIn.value = '';
  });

  /* ------------------------------------------------- making it plain WAV --
   *
   * Every browser records something different — webm/opus on Chrome and
   * Firefox, mp4/aac on Safari — and the transcription services do not
   * agree on which of those they will take. Rather than find that out in
   * somebody's face, whatever was recorded or chosen is decoded here and
   * re-encoded as 16 kHz mono WAV, which every one of them accepts.
   *
   * Nothing is lost by it: both services downsample to 16 kHz mono
   * themselves before they listen, so this is the same audio they would
   * have made, made earlier and in a container nobody argues about.
   *
   * If any step fails — an old browser, a format it cannot decode, a
   * Safari that will only run an OfflineAudioContext at 44.1 kHz — the
   * original blob is sent exactly as it was. A conversion that is an
   * optimisation must never be the thing that breaks the feature.
   */
  var RATE = 16000;

  function wavOf(samples, rate) {
    var n = samples.length;
    var buf = new ArrayBuffer(44 + n * 2);
    var view = new DataView(buf);
    function str(at, text) {
      for (var i = 0; i < text.length; i++) view.setUint8(at + i, text.charCodeAt(i));
    }
    str(0, 'RIFF');
    view.setUint32(4, 36 + n * 2, true);
    str(8, 'WAVEfmt ');
    view.setUint32(16, 16, true);        // PCM header length
    view.setUint16(20, 1, true);         // PCM
    view.setUint16(22, 1, true);         // mono
    view.setUint32(24, rate, true);
    view.setUint32(28, rate * 2, true);  // bytes per second
    view.setUint16(32, 2, true);         // bytes per frame
    view.setUint16(34, 16, true);        // bits per sample
    str(36, 'data');
    view.setUint32(40, n * 2, true);
    for (var i = 0; i < n; i++) {
      var v = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
    }
    return new Blob([view], { type: 'audio/wav' });
  }

  function toWav(blob) {
    return new Promise(function (done) {
      var AC = window.AudioContext || window.webkitAudioContext;
      var OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
      if (!AC || !OAC || !blob.arrayBuffer) { done(null); return; }
      var ctx;
      blob.arrayBuffer().then(function (raw) {
        ctx = new AC();
        return ctx.decodeAudioData(raw);
      }).then(function (audio) {
        try { ctx.close(); } catch (e) { /* already closed */ }
        var frames = Math.ceil(audio.duration * RATE);
        if (!frames) return null;
        var off = new OAC(1, frames, RATE);
        var src = off.createBufferSource();
        src.buffer = audio;
        src.connect(off.destination);
        src.start();
        return off.startRendering().then(function (done2) {
          return { audio: done2, seconds: Math.round(audio.duration) };
        });
      }).then(function (got) {
        done(got ? { blob: wavOf(got.audio.getChannelData(0), RATE),
                     seconds: got.seconds } : null);
      }).catch(function () {
        try { if (ctx) ctx.close(); } catch (e) { /* nothing to close */ }
        done(null);
      });
    });
  }

  /* ------------------------------------------------------------ sending -- */

  function send(blob, name, seconds) {
    say('Tidying the recording…');
    toWav(blob).then(function (made) {
      if (made) {
        upload(made.blob, name.replace(/\.[^.]+$/, '') + '.wav',
               made.seconds || seconds);
      } else {
        // Sent as recorded. The service may still take it, and being
        // told "your browser is too old" would be a worse answer than
        // simply trying.
        upload(blob, name, seconds);
      }
    });
  }

  function upload(blob, name, seconds) {
    if (blob.size > maxMb * 1024 * 1024) {
      // Said in minutes as well as megabytes: nobody chooses a recording
      // by its file size, and "9.4 MB" tells them nothing they can act on.
      fail('That recording comes to ' + (blob.size / 1024 / 1024).toFixed(1) +
           ' MB once it is tidied up, and the limit is ' + maxMb + ' MB — ' +
           'about five minutes of speech. Record it in two parts, or type ' +
           'the task out.');
      say('');
      busy(false);
      return;
    }
    busy(true);
    say('Writing it down…');
    show(blob);

    var body = new FormData();
    body.append('audio', blob, name);
    body.append('language', langIn.value || '');
    body.append('seconds', String(seconds || 0));

    fetch('/tasks/voice', { method: 'POST', body: body, credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        busy(false);
        // The recording is kept even when the words could not be got, so
        // the doer can listen to what was actually said.
        if (data.token) token.value = data.token;
        if (data.problem) fail(data.problem);
        if (!data.ok) { say(''); return; }
        say('');
        fill(data);
      })
      .catch(function () {
        busy(false);
        say('');
        fail('The transcript did not come back. Your recording is safe — ' +
             'try again, or type the task out.');
      });
  }

  function show(blob) {
    if (blobUrl) URL.revokeObjectURL(blobUrl);
    blobUrl = URL.createObjectURL(blob);
    player.src = blobUrl;
    out.hidden = false;
  }

  dropBtn.addEventListener('click', function () {
    token.value = '';
    out.hidden = true;
    if (blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = null; }
    player.removeAttribute('src');
    say('The recording will not be attached. Anything it wrote is still in ' +
        'the boxes below.');
  });

  /* ------------------------------------------------- putting it in place -- */

  function append(field, text) {
    if (!field || !text) return;
    var had = (field.value || '').trim();
    field.value = had ? had + '\n\n' + text : text;
  }

  function fill(data) {
    if (titleIn && !(titleIn.value || '').trim() && data.title) {
      titleIn.value = data.title;
    }
    append(detailsIn, data.text);
    drawWho(data.people || []);
    drawWhen(data.whens || []);
    drawHow(data.urgency || null);
    tally();
  }

  /* A chip is one suggestion. Some arrive already applied — the page
   * filled the form in because the recording was unmistakable — and
   * clicking an applied one puts it back. Either way the same click
   * toggles, so there is nothing to learn twice.
   */
  function chip(box, text, apply, undo, hint, applied) {
    var b = document.createElement('button');
    var on = !!applied;
    b.type = 'button';
    b.className = 'vn-chip';
    if (hint) b.title = hint;

    function paint() {
      b.classList.toggle('taken', on);
      b.textContent = text;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    b.addEventListener('click', function () {
      on = !on;
      if (on) { apply(); } else if (undo) { undo(); }
      paint();
      tally();
    });
    paint();
    box.appendChild(b);
    return b;
  }

  function label(box, text) {
    var s = document.createElement('span');
    s.className = 'm vn-label';
    s.textContent = text;
    box.appendChild(s);
  }

  /* One line saying what was filled in, so nobody has to notice it. */
  function tally() {
    var on = document.querySelectorAll('#vn .vn-chip.taken').length;
    doneNote.hidden = !on;
    doneNote.textContent = on
      ? 'Filled in from the recording — check it, and click a tick to undo.'
      : '';
  }

  function setDoer(id, want) {
    if (!picker) return;
    for (var i = 0; i < picker.options.length; i++) {
      if (String(picker.options[i].value) === String(id)) {
        picker.options[i].selected = !!want;
      }
    }
    // Tell the tag picker drawn on top of the select to redraw itself.
    picker.dispatchEvent(new Event('dp:refresh'));
    picker.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function drawWho(people) {
    whoBox.innerHTML = '';
    whoBox.hidden = !people.length;
    if (!people.length || !picker) return;
    var auto = people.length === 1 && people[0].apply;
    label(whoBox, auto ? 'Assigned to:'
                       : (people.length === 1 ? 'Heard a name:'
                                              : 'Heard a name — which one?'));
    people.forEach(function (p) {
      var put = function () { setDoer(p.id, true); };
      var off = function () { setDoer(p.id, false); };
      if (p.apply) put();
      chip(whoBox, p.name, put, off,
           'It heard "' + p.heard + '".', p.apply);
    });
  }

  function drawWhen(whens) {
    whenBox.innerHTML = '';
    whenBox.hidden = !whens.length;
    if (!whens.length || !dueIn) return;
    var was = dueIn.value;
    label(whenBox, whens.length === 1 && whens[0].apply
          ? 'Deadline set to:' : 'Heard a deadline:');
    whens.forEach(function (w) {
      var put = function () { dueIn.value = w.value; };
      var off = function () { dueIn.value = was; };
      if (w.apply) put();
      chip(whenBox, w.label, put, off,
           'It heard "' + w.phrase + '".', w.apply);
    });
  }

  function drawHow(u) {
    howBox.innerHTML = '';
    howBox.hidden = !u;
    if (!u || !prioIn) return;
    var was = prioIn.value;
    label(howBox, 'Priority:');
    var put = function () { prioIn.value = u.value; };
    var off = function () { prioIn.value = was; };
    if (u.apply) put();
    chip(howBox, u.label, put, off, 'It heard "' + u.phrase + '".', u.apply);
  }

})();
