/* Pick people as tags.
 *
 * Choose a name, it drops into the box with a cross on it, and the chooser
 * moves on to whoever is left. Click a cross to take somebody out.
 *
 * Every one of these is drawn ON TOP OF a real <select multiple> that is
 * already in the page, already named, already carrying the right values.
 * This file only hides it and keeps it in step. So if the file fails to
 * load, or script is off, the form still works — the browser's own
 * multi-select is sitting right there. A form that stops working because
 * one asset did not arrive is not a trade worth making.
 *
 * It runs on a delay and again after anything is added to the page, because
 * the FMS builder clones whole step cards long after this file has run.
 */
(function () {
  'use strict';

  function build(box) {
    if (box.dataset.wired === '1') return;
    var src = box.querySelector('.dp-src');
    if (!src) return;
    box.dataset.wired = '1';

    var one = box.dataset.one === '1';
    var mirror = box.querySelector('.dp-mirror');
    var count = box.querySelector('.picked');
    var addText = box.dataset.add || 'Add a person';

    // The native control stays in the DOM and keeps doing the real work.
    src.classList.add('dp-hidden');

    var shell = document.createElement('div');
    shell.className = 'dp-shell';
    shell.innerHTML =
      '<div class="dp-chips" role="list"></div>' +
      '<select class="dp-add"></select>';
    src.parentNode.insertBefore(shell, src.nextSibling);

    var chips = shell.querySelector('.dp-chips');
    var add = shell.querySelector('.dp-add');

    function options() {
      return Array.prototype.slice.call(src.options)
        .filter(function (o) { return o.value !== ''; });
    }

    function draw() {
      var all = options();
      var on = all.filter(function (o) { return o.selected; });

      chips.innerHTML = '';
      on.forEach(function (o) {
        var chip = document.createElement('span');
        chip.className = 'dp-chip';
        chip.setAttribute('role', 'listitem');
        var nm = document.createElement('span');
        nm.className = 'who';
        nm.textContent = o.textContent.trim();
        var x = document.createElement('button');
        x.type = 'button';           // never submit the form
        x.className = 'x';
        x.setAttribute('aria-label', 'Remove ' + o.textContent.trim());
        x.textContent = '×';
        x.addEventListener('click', function () {
          o.selected = false;
          if (one) src.value = '';
          draw();
        });
        chip.appendChild(nm);
        chip.appendChild(x);
        chips.appendChild(chip);
      });

      if (!on.length) {
        var none = document.createElement('span');
        none.className = 'dp-none';
        none.textContent = one ? 'Nobody chosen yet' : 'Nobody chosen yet — pick below';
        chips.appendChild(none);
      }

      // The chooser only ever offers people who are not already in the box,
      // so the same person cannot be added twice.
      var left = all.filter(function (o) { return !o.selected; });
      add.innerHTML = '';
      var head = document.createElement('option');
      head.value = '';
      head.textContent = left.length
        ? (one && on.length ? 'Change to…' : addText + '…')
        : 'Everyone is already chosen';
      add.appendChild(head);
      left.forEach(function (o) {
        var opt = document.createElement('option');
        opt.value = o.value;
        var sub = o.dataset.sub || '';
        opt.textContent = o.textContent.trim() + (sub ? '  —  ' + sub : '');
        add.appendChild(opt);
      });
      add.disabled = !left.length;

      if (count) {
        if (!on.length) count.textContent = '';
        else if (on.length === 1) count.textContent = '— ' + on[0].textContent.trim();
        else count.textContent = '— ' + on.length + ' people, one task each';
      }
      if (mirror) {
        mirror.value = on.map(function (o) { return o.value; }).join(',');
      }
      // Anything watching the form — the FMS draft saver, for one — should
      // hear about this the same as it hears about a typed character.
      src.dispatchEvent(new Event('change', { bubbles: true }));
    }

    add.addEventListener('change', function () {
      if (!add.value) return;
      options().forEach(function (o) {
        if (o.value === add.value) o.selected = true;
        else if (one) o.selected = false;    // one person means one person
      });
      draw();
    });

    // Something else changed the underlying select — a restored draft, say.
    src.addEventListener('dp:refresh', draw);
    draw();
  }

  function sweep() {
    document.querySelectorAll('.doerpick').forEach(build);
  }

  sweep();
  // Step cards are cloned into the page long after this file has run.
  if (window.MutationObserver) {
    new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        if (muts[i].addedNodes.length) { sweep(); return; }
      }
    }).observe(document.body, { childList: true, subtree: true });
  }
})();
