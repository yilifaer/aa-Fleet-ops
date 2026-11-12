// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// Live preview of the colour on the fleet type edit page. Saving checks the colour again on the server; this only
// shows it: the dot pill, a filled sample with black or white text (WCAG contrast) and a warning when a custom
// colour is hard to see on the page background of the light (Flatly) or dark (Darkly) theme.
(() => {
  const card = document.querySelector('[data-fo-type-preview]');
  const choice = document.getElementById('id_color_choice');
  const custom = document.getElementById('id_custom_color');
  const name = document.getElementById('id_name');
  if (!card || !choice || !custom) return;
  const pill = document.getElementById('fleet-type-preview-pill');
  const filled = document.getElementById('fleet-type-preview-filled');
  const autoNote = document.getElementById('fleet-type-preview-auto');
  const warning = document.getElementById('fleet-type-preview-warning');
  // Page backgrounds as channel values (0-1): white (Flatly) and 34/255 grey (Darkly); same as fleetops/ui.py.
  const BACKGROUNDS = [['light', [1, 1, 1]], ['dark', [34 / 255, 34 / 255, 34 / 255]]];
  const MIN_CONTRAST = 2;

  function parse(text) {
    const match = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec((text || '').trim());
    if (!match) return null;
    let digits = match[1].toLowerCase();
    if (digits.length === 3) digits = digits.replace(/./g, (c) => c + c);
    return digits.match(/../g).map((pair) => parseInt(pair, 16) / 255);
  }
  function luminance(rgb) {
    const [r, g, b] = rgb.map((v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }
  function ratio(a, b) {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  }

  function show() {
    const value = choice.value;
    let dot = '';
    let rgb = null;
    autoNote.hidden = true;
    warning.hidden = true;
    warning.textContent = '';
    if (value === 'custom') {
      rgb = parse(custom.value);
      if (rgb) dot = '#' + rgb.map((v) => Math.round(v * 255).toString(16).padStart(2, '0')).join('');
    } else if (/^theme-[1-6]$/.test(value)) {
      dot = 'var(--fo-type-' + value.slice(-1) + ')';
    } else {
      const tone = parseInt(card.dataset.autoTone, 10);
      if (tone > 0) dot = 'var(--fo-type-' + tone + ')';
      else autoNote.hidden = false;
    }
    if (name && name.value.trim()) pill.textContent = name.value.trim();
    pill.style.setProperty('--fo-dot', dot || 'var(--fo-text-2)');
    filled.style.background = dot || 'var(--fo-surface-2)';
    if (rgb) {
      const ink = ratio(rgb, [0, 0, 0]) >= ratio(rgb, [1, 1, 1]) ? [0, 0, 0] : [255, 255, 255];
      filled.style.color = 'rgb(' + ink.join(',') + ')';
      const poor = BACKGROUNDS.filter(([, background]) => ratio(rgb, background) < MIN_CONTRAST).map(([theme]) => theme);
      if (poor.length) {
        warning.textContent = poor.map((theme) => card.dataset['warn' + theme[0].toUpperCase() + theme.slice(1)]).join(' ');
        warning.hidden = false;
      }
    }
    filled.parentElement.hidden = !rgb;
  }
  [choice, custom, name].forEach((el) => el && el.addEventListener('input', show));
  choice.addEventListener('change', show);
  show();
})();
