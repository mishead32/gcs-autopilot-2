/* Pick people as tags.
 *
 * Type a few letters and the matching names drop down; click one, or press
 * Enter, and it becomes a tag with a cross on it. An empty box shows
 * everybody, so it works as a plain list for anyone who would rather not
 * type. Click a cross to take somebody out.
 *
 * Names only. Job title and company were in this list once and they were
 * noise: everybody in it is there for the same reason, and the extra words
 * turned eighteen readable rows into eighteen unreadable ones.
 *
 * Every one of these is drawn ON TOP OF a real <select multiple> that is
 * already in the page, already named, already carrying the right values.
 * This file only hides it and keeps it in step. So if the file fails to
 * load, or script is off, the form still works — the browser's own
 * multi-select is sitting right there. A form that stops working because
 * one asset did not arrive is not a trade worth making.
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
    var prompt = box.dataset.add || 'Type a name, or pick from the list';

    // The native control stays in the DOM and keeps doing the real work.
    src.classList.add('dp-hidden');

    var shell = document.createElement('div');
    shell.className = 'dp-shell';
    shell.innerHTML =
      '<div class="dp-chips" role="list"></div>' +
      '<div class="dp-find">' +
      '<input type="text" class="dp-type" autocomplete="off" spellcheck="false" ' +
      'role="combobox" aria-expanded="false" aria-autocomplete="list">' +
      '<div class="dp-menu" role="listbox" hidden></div>' +
      '</div>';
    src.parentNode.insertBefore(shell, src.nextSibling);

    var chips = shell.querySelector('.dp-chips');
    var typer = shell.querySelector('.dp-type');
    var menu = shell.querySelector('.dp-menu');
    typer.placeholder = prompt;

    function options() {
      return Array.prototype.slice.call(src.options)
        .filter(function (o) { return o.value !== ''; });
    }

    function chosen() {
      return options().filter(function (o) { return o.selected; });
    }

    /* ------------------------------------------------------- the tags --- */
    function draw() {
      var on = chosen();

      chips.innerHTML = '';
      on.forEach(function (o) {
        var chip = document.createElement('span');
        chip.className = 'dp-chip';
        chip.setAttribute('role', 'listitem');

        var nm = document.createElement('span');
        nm.className = 'who';
        nm.textContent = o.textContent.trim();

        var x = document.createElement('button');
        x.type = 'button';                 // never submit the form
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
        none.textContent = 'Nobody chosen yet';
        chips.appendChild(none);
      }

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
      if (!menu.hidden) openMenu();
    }

    /* ------------------------------------------------ the search list --- */
    function openMenu() {
      var q = typer.value.trim().toLowerCase();
      var left = options().filter(function (o) { return !o.selected; });
      var hits = q
        ? left.filter(function (o) {
            return o.textContent.trim().toLowerCase().indexOf(q) !== -1;
          })
        : left;

      menu.innerHTML = '';
      if (!hits.length) {
        var miss = document.createElement('div');
        miss.className = 'dp-miss';
        miss.textContent = left.length ? 'No name matches that'
                                       : 'Everyone is already chosen';
        menu.appendChild(miss);
      } else {
        hits.forEach(function (o, i) {
          var row = document.createElement('button');
          row.type = 'button';             // never submit the form
          row.className = 'dp-opt' + (i === 0 ? ' on' : '');
          row.setAttribute('role', 'option');
          row.textContent = o.textContent.trim();
          // mousedown, not click: the box loses focus first and a blur
          // handler would close the list out from under the click.
          row.addEventListener('mousedown', function (e) {
            e.preventDefault();
            pick(o);
          });
          row.addEventListener('mouseenter', function () {
            menu.querySelectorAll('.on').forEach(function (r) {
              r.classList.remove('on');
            });
            row.classList.add('on');
          });
          menu.appendChild(row);
        });
      }
      menu.hidden = false;
      typer.setAttribute('aria-expanded', 'true');
    }

    function shut() {
      menu.hidden = true;
      typer.setAttribute('aria-expanded', 'false');
    }

    function pick(o) {
      options().forEach(function (other) {
        if (other === o) other.selected = true;
        else if (one) other.selected = false;   // one person means one person
      });
      typer.value = '';
      draw();
      shut();
      typer.focus();
    }

    function move(step) {
      var rows = Array.prototype.slice.call(menu.querySelectorAll('.dp-opt'));
      if (!rows.length) return;
      var at = -1;
      rows.forEach(function (r, i) { if (r.classList.contains('on')) at = i; });
      rows.forEach(function (r) { r.classList.remove('on'); });
      var next = (at + step + rows.length) % rows.length;
      rows[next].classList.add('on');
      rows[next].scrollIntoView({ block: 'nearest' });
    }

    typer.addEventListener('focus', openMenu);
    typer.addEventListener('input', openMenu);
    typer.addEventListener('blur', function () { setTimeout(shut, 130); });
    typer.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (menu.hidden) openMenu(); else move(1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        move(-1);
      } else if (e.key === 'Escape') {
        shut();
      } else if (e.key === 'Enter') {
        // Enter in this box picks a name. Without this it would submit the
        // whole form half-filled.
        e.preventDefault();
        var on = menu.querySelector('.dp-opt.on');
        if (on && !menu.hidden) on.dispatchEvent(new MouseEvent('mousedown'));
      } else if (e.key === 'Backspace' && !typer.value) {
        // Backspace on an empty box takes the last tag off, the way every
        // other tag box people use behaves.
        var picked = chosen();
        if (picked.length) {
          picked[picked.length - 1].selected = false;
          draw();
        }
      }
    });

    // Something else changed the underlying select — a restored draft, or
    // the step card clearing somebody who just became the primary doer.
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
