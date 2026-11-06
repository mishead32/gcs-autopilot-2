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
  var errBox = document.getElementById('vnErr');
  var token = document.getElementById('vnToken');

  var titleIn = document.querySelector('input[name="title"]');
  var detailsIn = document.querySelector('textarea[name="details"]');
  var dueIn = document.querySelector('input[name="due_at"]');
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
        // A hard stop, so a forgotten recording cannot become a 40 MB
        // upload that is refused after five minutes of waiting.
        if (s >= 600) stopBtn.click();
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

  /* ------------------------------------------------------------ sending -- */

  function send(blob, name, seconds) {
    if (blob.size > maxMb * 1024 * 1024) {
      fail('That recording is ' + (blob.size / 1024 / 1024).toFixed(1) +
           ' MB and the limit is ' + maxMb + ' MB. Record it in two parts, ' +
           'or type the task out.');
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
  }

  function chip(box, text, onClick, hint) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'vn-chip';
    b.textContent = text;
    if (hint) b.title = hint;
    b.addEventListener('click', function () {
      onClick();
      b.classList.add('taken');
      b.disabled = true;
    });
    box.appendChild(b);
    return b;
  }

  function label(box, text) {
    var s = document.createElement('span');
    s.className = 'm vn-label';
    s.textContent = text;
    box.appendChild(s);
  }

  function drawWho(people) {
    whoBox.innerHTML = '';
    whoBox.hidden = !people.length;
    if (!people.length || !picker) return;
    label(whoBox, people.length === 1 ? 'Heard a name:' : 'Heard a name — which one?');
    people.forEach(function (p) {
      chip(whoBox, p.name, function () {
        for (var i = 0; i < picker.options.length; i++) {
          if (String(picker.options[i].value) === String(p.id)) {
            picker.options[i].selected = true;
          }
        }
        // Tell the tag picker drawn on top of the select to redraw.
        picker.dispatchEvent(new Event('dp:refresh'));
        picker.dispatchEvent(new Event('change', { bubbles: true }));
      }, 'It heard "' + p.heard + '". Click to assign to ' + p.name + '.');
    });
  }

  function drawWhen(whens) {
    whenBox.innerHTML = '';
    whenBox.hidden = !whens.length;
    if (!whens.length || !dueIn) return;
    label(whenBox, 'Heard a deadline:');
    whens.forEach(function (w) {
      chip(whenBox, w.label, function () {
        dueIn.value = w.value;
      }, 'It heard "' + w.phrase + '". Click to set the deadline.');
    });
  }
})();
