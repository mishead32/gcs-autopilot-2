/* Mark a task complete without leaving the list.
 *
 * The button in the last column of every task list opens this box. It loads
 * the real submit form from the server (GET /tasks/{id}/mark) and posts it to
 * the real submit route, so there is exactly one set of rules about closing a
 * task — this is a different doorway into the same room, not a second room.
 *
 * Two things it does that the task page does not:
 *
 *   return_to   the form is sent back to whatever list it was opened from,
 *               on the same tab and filter, because that is where the person
 *               was working.
 *
 *   uploads     always through fetch. On the task page a picked file can ride
 *               an ordinary form post, because navigating away is harmless
 *               there. Here it would tear the box off the screen mid-sentence
 *               and lose the note they had started typing.
 *
 * If the browser has no <dialog> support, the button simply goes to the task
 * page. Nobody is left unable to close their work.
 */
(function () {
  var dlg = document.getElementById('markDialog');
  if (!dlg) return;

  var body = dlg.querySelector('.mb-body');
  var closeBtn = dlg.querySelector('.mb-close');
  var current = null;

  function canModal() { return typeof dlg.showModal === 'function'; }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('[data-mark]');
    if (!btn) return;
    e.preventDefault();
    var id = btn.getAttribute('data-mark');
    if (!canModal()) { window.location.href = '/tasks/' + id; return; }
    open(id);
  });

  function open(id) {
    current = id;
    pasteInto = null;            // the box being replaced no longer owns paste
    body.innerHTML = '<div class="mb-load">Opening…</div>';
    dlg.showModal();
    fetch('/tasks/' + id + '/mark', {
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'fetch' }
    })
      .then(function (r) {
        if (!r.ok) throw new Error('This task could not be opened here.');
        return r.text();
      })
      .then(function (html) {
        body.innerHTML = html;
        wire();
      })
      .catch(function (err) {
        body.innerHTML = '<div class="mb-load bad">' + esc(err.message) +
          '<br><a href="/tasks/' + esc(id) + '">Open the task page instead</a></div>';
      });
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;',
               '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function close() { current = null; pasteInto = null; dlg.close(); }

  if (closeBtn) closeBtn.addEventListener('click', close);

  // Clicking the dark area outside the box closes it. The check is on the
  // dialog element itself: a click that lands on anything inside bubbles up
  // with that child as its target, so real work is never dismissed by it.
  dlg.addEventListener('click', function (e) { if (e.target === dlg) close(); });

  /* ------------------------------------------------------------ wiring --- */
  function wire() {
    var form = body.querySelector('#markForm');
    if (!form) return;

    // Come back to this exact list, tab and filter.
    var ret = form.querySelector('.returnto');
    if (ret) ret.value = window.location.pathname + window.location.search;

    // Submitting takes a moment; a second click would send it twice.
    form.addEventListener('submit', function () {
      setTimeout(function () {
        form.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
      }, 0);
    });

    var up = form.querySelector('.uploader');
    if (up) wireUploads(form, up);

    var first = form.querySelector('input:not([type=hidden]), textarea');
    if (first) first.focus();
  }

  /* ------------------------------------------------------------ uploads -- */
  function wireUploads(form, box) {
    var taskId = form.dataset.task;
    var mode = form.dataset.mode;
    var maxMb = parseFloat(form.dataset.max || '10');
    var input = box.querySelector('input[type=file]');
    var drop = box.querySelector('.drop');
    var paste = box.querySelector('.paste-zone');
    var list = box.querySelector('.up-list');

    function row(name) {
      var el = document.createElement('div');
      el.className = 'up-row';
      el.innerHTML = '<span class="nm"></span><span class="bar"><i></i></span>' +
        '<span class="st">waiting</span>';
      el.querySelector('.nm').textContent = name;
      list.appendChild(el);
      return {
        pct: function (p) { el.querySelector('.bar > i').style.width = p + '%'; },
        say: function (t, cls) {
          el.querySelector('.st').textContent = t;
          if (cls) el.className = 'up-row ' + cls;
        }
      };
    }

    // One file attached is all the proof gate asks for, so the buttons come
    // on as soon as the first one lands rather than after the whole batch.
    function proofArrived() {
      form.dataset.has = String((parseInt(form.dataset.has, 10) || 0) + 1);
      var need = form.querySelector('.needbox');
      if (need) {
        need.className = 'proof ok';
        need.textContent = 'Proof attached — you can submit.';
      }
      form.querySelectorAll('.markacts button[disabled]').forEach(function (b) {
        b.disabled = false;
      });
    }

    function xhrSend(method, url, payload, headers, onProgress) {
      return new Promise(function (resolve, reject) {
        var xhr = new XMLHttpRequest();
        xhr.open(method, url, true);
        Object.keys(headers || {}).forEach(function (h) {
          xhr.setRequestHeader(h, headers[h]);
        });
        xhr.upload.onprogress = function (e) {
          if (e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100));
        };
        xhr.onload = function () {
          if (xhr.status >= 200 && xhr.status < 300) return resolve(xhr.responseText);
          var msg = 'Upload failed';
          try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (err) {}
          reject(new Error(msg));
        };
        xhr.onerror = function () { reject(new Error('Network dropped mid-upload')); };
        xhr.send(payload);
      });
    }

    function json(url, data) {
      return fetch(url, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      }).then(function (r) {
        if (r.ok) return r.json();
        return r.json().catch(function () { return {}; }).then(function (d) {
          throw new Error(d.detail || 'Upload failed');
        });
      });
    }

    function sendOne(file) {
      var r = row(file.name);
      if (file.size > maxMb * 1024 * 1024) {
        r.say((file.size / 1048576).toFixed(1) + ' MB — over the ' + maxMb + ' MB limit', 'bad');
        return Promise.resolve(false);
      }
      var type = file.type || 'application/octet-stream';
      var job;
      if (mode === 's3') {
        r.say('starting');
        job = json('/tasks/' + taskId + '/attach/ticket',
                   { filename: file.name, content_type: type, size: file.size })
          .then(function (t) {
            r.say('uploading');
            return xhrSend(t.method || 'PUT', t.url, file, t.headers, r.pct)
              .then(function () { return t.key; });
          })
          .then(function (key) {
            r.pct(100); r.say('saving');
            return json('/tasks/' + taskId + '/attach/confirm',
                        { key: key, filename: file.name, content_type: type });
          });
      } else {
        r.say('uploading');
        var fd = new FormData();
        fd.append('file', file, file.name);
        job = xhrSend('POST', '/tasks/' + taskId + '/attach/upload', fd, null, r.pct);
      }
      return job.then(function () {
        r.pct(100); r.say('attached', 'ok');
        proofArrived();
        return true;
      }).catch(function (e) {
        r.say(e.message || 'failed', 'bad');
        return false;
      });
    }

    function handle(files) {
      if (!files || !files.length) return;
      var chain = Promise.resolve();
      Array.prototype.forEach.call(files, function (f) {
        chain = chain.then(function () { return sendOne(f); });
      });
    }

    input.addEventListener('change', function () { handle(input.files); });
    drop.addEventListener('click', function () { input.click(); });
    ['dragenter', 'dragover'].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault(); drop.classList.add('over');
      });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault(); drop.classList.remove('over');
      });
    });
    drop.addEventListener('drop', function (e) {
      if (e.dataTransfer && e.dataTransfer.files) handle(e.dataTransfer.files);
    });

    /* A screenshot on the clipboard is a nameless blob, so we name it. A
     * folder of files all called "image.png" helps nobody. */
    function stamp(ext) {
      var d = new Date(), p = function (n) { return (n < 10 ? '0' : '') + n; };
      return 'screenshot-' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate())
           + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + ext;
    }

    function fromClipboard(e) {
      var cd = e.clipboardData || window.clipboardData;
      if (!cd) return null;
      var out = [];
      if (cd.files && cd.files.length) {
        Array.prototype.forEach.call(cd.files, function (f) {
          out.push((f.type && f.type.indexOf('image/') === 0 && !f.name)
            ? new File([f], stamp('.png'), { type: f.type }) : f);
        });
        return out;
      }
      var items = cd.items || [];
      for (var i = 0; i < items.length; i++) {
        if (items[i].kind !== 'file') continue;
        var blob = items[i].getAsFile();
        if (!blob) continue;
        var ext = (blob.type === 'image/jpeg') ? '.jpg'
                : (blob.type === 'image/webp') ? '.webp' : '.png';
        out.push(new File([blob], blob.name || stamp(ext), { type: blob.type }));
      }
      return out;
    }

    // Hand the one clipboard listener below whatever the open box wants done
    // with a pasted screenshot. Registering a listener per box instead would
    // stack them up: open the box twice and one paste would upload twice.
    pasteInto = { zone: paste, take: handle, read: fromClipboard };

    if (paste) paste.addEventListener('click', function () { paste.focus(); });
  }

  /* One clipboard listener for the life of the page, pointed at whichever
   * box is open. Text pasted into the note stays ordinary pasted text. */
  var pasteInto = null;
  dlg.addEventListener('paste', function (e) {
    if (!dlg.open || !pasteInto) return;
    var cd = e.clipboardData;
    var text = cd && cd.getData && cd.getData('text/plain');
    var el = e.target;
    var typing = el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA');
    if (typing && text && el !== pasteInto.zone) return;
    var files = pasteInto.read(e);
    if (!files || !files.length) return;
    e.preventDefault();
    var zone = pasteInto.zone;
    if (zone) {
      zone.classList.add('hit');
      setTimeout(function () { zone.classList.remove('hit'); }, 700);
    }
    pasteInto.take(files);
  });
})();
