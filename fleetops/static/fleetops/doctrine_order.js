// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// Doctrine ship order editor. The form works without this file (the buttons post the form and the page comes back with
// the ship moved); with it the ships move in place and the preview follows every change.
(() => {
  const form = document.querySelector('form[data-doctrine-order]');
  if (!form) return;
  const preview = form.querySelector('[data-order-preview]');

  const ships = () => Array.from(form.querySelectorAll('[data-order-ship]'));
  const marks = () => Array.from(form.querySelectorAll('[data-order-mark]'));

  function refresh() {
    const rows = ships();
    const picked = marks();
    let text = '';
    rows.forEach((row, index) => {
      const name = row.querySelector('[data-order-name]').textContent;
      text += index ? ` ${picked[index - 1].value} ${name}` : name;
      const up = row.querySelector('[data-order-move="up"]');
      const down = row.querySelector('[data-order-move="down"]');
      up.value = `up:${index}`;
      down.value = `down:${index}`;
      up.disabled = index === 0;
      down.disabled = index === rows.length - 1;
    });
    if (preview) preview.textContent = text;
  }

  function swap(index, other) {
    const rows = ships();
    if (other < 0 || other >= rows.length) return;
    // The marks stay between the same positions; only the ships change places.
    const a = rows[index];
    const b = rows[other];
    const aName = a.querySelector('[data-order-name]');
    const bName = b.querySelector('[data-order-name]');
    const aInput = a.querySelector('input[name="ship"]');
    const bInput = b.querySelector('input[name="ship"]');
    [aName.textContent, bName.textContent] = [bName.textContent, aName.textContent];
    [aInput.value, bInput.value] = [bInput.value, aInput.value];
    refresh();
    const target = rows[other].querySelector(`[data-order-move="${other < index ? 'up' : 'down'}"]`);
    if (target && !target.disabled) target.focus();
  }

  form.addEventListener('click', (event) => {
    const button = event.target.closest('[data-order-move]');
    if (!button || button.disabled) return;
    event.preventDefault();
    const index = ships().indexOf(button.closest('[data-order-ship]'));
    swap(index, button.dataset.orderMove === 'up' ? index - 1 : index + 1);
  });
  form.addEventListener('change', (event) => {
    if (event.target.matches('[data-order-mark]')) refresh();
  });
  refresh();
})();
