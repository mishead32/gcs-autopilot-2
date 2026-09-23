/* Attachment uploader — choose a file, drag one in, or just paste.
 *
 * Two ways a file reaches storage:
 *
 *   bucket configured  ask the app for a signed URL -> PUT the bytes straight
 *                      to storage -> tell the app it landed. The web server
 *                      never carries the file, so big photos work even on
 *                      hosts that cap request bodies.
 *
 *   no bucket          POST the file to the app, which writes it to disk.
 *
 * Paste matters more than it looks: most proof here is a screenshot, and
 * Ctrl+V is one action where "save the screenshot, find it, upload it" is
 * four. A clipboard image is a Blob with no entry in any file input, so it
 * cannot ride the plain form post — it always goes through fetch.
 */
(function () {
  var box = document.getElementById('uploader');
  if (!box) return;

  var taskId = box.dataset.task;
  var mode = box.dataset.mode;
  var maxMb = parseFloat(box.dataset.max || '10');
  var input = box.querySelector('input[type=file]');
  var drop = box.querySelector('.drop');
  var pasteBox = box.querySelector('.paste-zone');
  var list = box.querySelector('.up-list');
  var localForm = document.getElementById('localUploadForm');

  function row(name) {
    var el = document.createElement('div');
    el.className = 'up-row';
    el.innerHTML = '<span class="nm"></span>' +
      '<span class="bar"><i></i></span><span class="st">waiting</span>';
    el.querySelector('.nm').textContent = name;
    list.appendChild(el);
    return {
      el: el,
      pct: function (p) { el.querySelector('.bar > i').style.width = p + '%'; },
      say: function (t, cls) {
        var s = el.querySelector('.st');
        s.textContent = t;
        if (cls) el.className = 'up-row ' + cls;
      }
    };
  }

  function put(url, headers, file, onProgress) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open('PUT', url, true);
      Object.keys(headers || {}).forEach(function (h) {
        xhr.setRequestHeader(h, headers[h]);
      });
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100));
      };
      xhr.onload = function () {
        (xhr.status >= 200 && xhr.status < 300)
          ? resolve()
          : reject(new Error('Storage rejected the file (' + xhr.status + ')'));
      };
      xhr.onerror = function () { reject(new Error('Network dropped mid-upload')); };
      xhr.send(file);
    });
  }

  function unwrap(r) {
    if (r.ok) return r.json();
    return r.json().catch(function () { return {}; }).then(function (d) {
      throw new Error(d.detail || 'Upload failed');
    });
  }

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(unwrap);
  }

  /* Straight to the app's disk. Used for pastes always, and for picked files
   * when there is no bucket. XHR rather than fetch so there is a progress bar. */
  function postFile(url, file, onProgress) {
    return new Promise(function (resolve, reject) {
      var fd = new FormData();
      fd.append('file', file, file.name);
      var xhr = new XMLHttpRequest();
      xhr.open('POST', url, true);
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100));
      };
      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 300) return resolve();
        var msg = 'Upload failed';
        try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (e) {}
        reject(new Error(msg));
      };
      xhr.onerror = function () { reject(new Error('Network dropped mid-upload')); };
      xhr.send(fd);
    });
  }

  function sendOne(file) {
    var r = row(file.name);

    if (file.size > maxMb * 1024 * 1024) {
      r.say((file.size / 1048576).toFixed(1) + ' MB — over the ' + maxMb + ' MB limit', 'bad');
      return Promise.resolve(false);
    }

    var type = file.type || 'application/octet-stream';
    r.say('starting');

    var job;
    if (mode === 's3') {
      job = post('/tasks/' + taskId + '/attach/ticket',
                 { filename: file.name, content_type: type, size: file.size })
        .then(function (t) {
          r.say('uploading');
          return put(t.url, t.headers, file, r.pct).then(function () { return t.key; });
        })
        .then(function (key) {
          r.pct(100);
          r.say('saving');
          return post('/tasks/' + taskId + '/attach/confirm',
                      { key: key, filename: file.name, content_type: type });
        });
    } else {
      r.say('uploading');
      job = postFile('/tasks/' + taskId + '/attach/upload', file, r.pct);
    }

    return job
      .then(function () {
        r.pct(100);
        r.say('attached', 'ok');
        return true;
      })
      .catch(function (e) {
        r.say(e.message || 'failed', 'bad');
        return false;
      });
  }

  function handle(files, viaFetch) {
    if (!files || !files.length) return;

    // Files picked from the disk with no bucket: the plain form post is
    // simplest and needs no JavaScript to succeed. Pastes can't use it.
    if (mode !== 's3' && !viaFetch) {
      if (localForm) { localForm.submit(); return; }
    }

    var any = false;
    var chain = Promise.resolve();
    Array.prototype.forEach.call(files, function (f) {
      chain = chain.then(function () {
        return sendOne(f).then(function (ok) { any = any || ok; });
      });
    });
    chain.then(function () {
      if (any) setTimeout(function () { window.location.reload(); }, 900);
    });
  }

  input.addEventListener('change', function () { handle(input.files); });

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
  drop.addEventListener('click', function () { input.click(); });

  /* --------------------------------------------------------------- paste ---
   * Chrome, Edge and Firefox all put a screenshot on the clipboard as an
   * image/* item with an empty name, so we name it ourselves — a folder full
   * of "image.png" helps nobody.
   */
  function stamp(ext) {
    var d = new Date(), p = function (n) { return (n < 10 ? '0' : '') + n; };
    return 'screenshot-' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate())
         + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + ext;
  }

  function fromClipboard(e) {
    var cd = e.clipboardData || window.clipboardData;
    if (!cd) return null;
    var out = [];

    // Modern browsers: real File objects on .files
    if (cd.files && cd.files.length) {
      Array.prototype.forEach.call(cd.files, function (f) {
        if (f.type && f.type.indexOf('image/') === 0 && !f.name) {
          out.push(new File([f], stamp('.png'), { type: f.type }));
        } else {
          out.push(f);
        }
      });
      return out;
    }

    // Older path: walk the items
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

  function onPaste(e) {
    var files = fromClipboard(e);
    if (!files || !files.length) return;
    e.preventDefault();
    if (pasteBox) pasteBox.classList.add('hit');
    handle(files, true);       // always fetch — a Blob can't ride a form post
    setTimeout(function () {
      if (pasteBox) pasteBox.classList.remove('hit');
    }, 700);
  }

  // Paste anywhere on the page, except while typing in a note or a text field
  // — pasting text into a textarea should stay ordinary pasting.
  document.addEventListener('paste', function (e) {
    var el = e.target;
    var typing = el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
                        || el.isContentEditable);
    if (typing && el !== pasteBox) {
      // still take it if the clipboard holds an image and no text
      var cd = e.clipboardData;
      var text = cd && cd.getData && cd.getData('text/plain');
      if (text) return;
    }
    onPaste(e);
  });

  if (pasteBox) {
    pasteBox.addEventListener('click', function () { pasteBox.focus(); });
  }
})();
