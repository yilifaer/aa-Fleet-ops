// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// In-page WCAG 2.x contrast checker (no dependency). Injected into the page by run.js; exposes
// window.__foContrast.run(options) -> {checked, exempt, findings}.
//
// What is checked: every element with text of its own inside the FleetOps container, the toast stack, the confirm
// dialog, open modals, open dropdown menus and tooltips, plus the counts FleetOps adds to Alliance Auth's menu bar.
// Text must reach 4.5:1 (3:1 for large text: 24px, or 18.66px and bold). Disabled controls, in-game and Discord
// look-alikes and elements marked data-ui-check-skip="<reason>" are exempt and only counted.
// The background is found by compositing every ancestor's background colour from the root down; a gradient
// is checked at each of its colour stops and at their average; a background image is reported as "manual".
(() => {
  'use strict';

  const colourCache = new Map();

  function rgba(css) {
    // A computed colour -> [r, g, b, a] (r, g, b in 0..255, a in 0..1).
    if (colourCache.has(css)) return colourCache.get(css);
    let out = null;
    let m;
    const alpha = (v) => (v === undefined ? 1 : (v.endsWith('%') ? parseFloat(v) / 100 : parseFloat(v)));
    if ((m = css.match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/))) {
      out = [+m[1], +m[2], +m[3], alpha(m[4])];
    } else if ((m = css.match(/^color\(srgb\s+([\d.e-]+)\s+([\d.e-]+)\s+([\d.e-]+)(?:\s*\/\s*([\d.]+%?))?\s*\)$/))) {
      out = [m[1] * 255, m[2] * 255, m[3] * 255, alpha(m[4])];
    } else {
      // Any other syntax (a name, another colour space): let the browser resolve it on a probe element.
      // A canvas round trip would lose precision at low alpha, so it is not used.
      const probe = document.createElement('i');
      probe.style.color = css;
      document.body.append(probe);
      const resolved = getComputedStyle(probe).color;
      probe.remove();
      out = resolved !== css && resolved ? rgba(resolved) : [0, 0, 0, 1];
    }
    colourCache.set(css, out);
    return out;
  }

  function over(top, bottom) {
    const a = top[3] + bottom[3] * (1 - top[3]);
    if (!a) return [0, 0, 0, 0];
    return [0, 1, 2].map((i) => (top[i] * top[3] + bottom[i] * bottom[3] * (1 - top[3])) / a).concat(a);
  }

  function luminance(c) {
    const [r, g, b] = c.slice(0, 3).map((v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }

  function ratio(a, b) {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  }

  function gradientStops(image) {
    if (!image || image === 'none' || !image.includes('gradient')) return [];
    return (image.match(/(rgba?\([^)]*\)|color\([^)]*\))/gi) || []).map(rgba);
  }

  // Every colour the background behind `el` can have: solid layers composited from the root, branched at gradients.
  function backgrounds(el) {
    const chain = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) chain.unshift(n);
    let candidates = [[255, 255, 255, 1]];
    let image = false;
    for (const node of chain) {
      const cs = getComputedStyle(node);
      const bg = rgba(cs.backgroundColor);
      const stops = gradientStops(cs.backgroundImage);
      // Small inline images (a select's arrow) sit beside the text, not behind it.
      if (/url\((?!['"]?data:)/.test(cs.backgroundImage)) image = true;
      candidates = candidates.map((c) => over(bg, c));
      if (stops.length) {
        const average = stops.reduce((acc, s) => acc.map((v, i) => v + s[i] / stops.length), [0, 0, 0, 0]);
        candidates = candidates.flatMap((c) => stops.concat([average]).map((s) => over(s, c)));
      }
    }
    return { candidates, image, gradient: chain.some((n) => gradientStops(getComputedStyle(n).backgroundImage).length) };
  }

  function opacityOf(el) {
    let o = 1;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) o *= parseFloat(getComputedStyle(n).opacity);
    return o;
  }

  function describe(el) {
    // A short path that finds the element again: tag#id.class, with the parents up to the FleetOps container.
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && parts.length < 4; n = n.parentElement) {
      let part = n.tagName.toLowerCase();
      if (n.id) part += '#' + n.id;
      const classes = [...n.classList].filter((c) => !/^\d/.test(c)).slice(0, 3);
      if (classes.length) part += '.' + classes.join('.');
      parts.unshift(part);
      if (n.id || n.classList.contains('fleetops')) break;
    }
    return parts.join(' > ');
  }

  function ownText(el) {
    return [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(' ').replace(/\s+/g, ' ').trim();
  }

  const FORM_CONTROLS = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=range]), textarea, select';
  const SCOPES = [
    '.fleetops', '#fo-toasts', '#fo-confirm', '.modal.show', '.dropdown-menu.show', '.tooltip.show',
  ];
  // What FleetOps adds to Alliance Auth's menu bar.
  const NAV_SELECTOR = '#nav-left .badge, #nav-left .fo-count, #nav-left .fo-pill, #nav-left .fo-eve-clock';
  const EXEMPT = ':disabled, [aria-disabled="true"], .disabled, fieldset:disabled, .fo-ingame, .fo-discord, [data-ui-check-skip]';

  function visible(el) {
    if (typeof el.checkVisibility === 'function') {
      if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    }
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  }

  function candidates() {
    const found = new Set();
    for (const scope of SCOPES) {
      for (const root of document.querySelectorAll(scope)) {
        found.add(root);
        root.querySelectorAll('*').forEach((el) => found.add(el));
      }
    }
    document.querySelectorAll(NAV_SELECTOR).forEach((el) => found.add(el));
    return [...found];
  }

  function check(el, sample, kind) {
    const cs = getComputedStyle(el);
    const { candidates: bgs, image, gradient } = backgrounds(el);
    const foreground = rgba(kind === 'placeholder' ? getComputedStyle(el, '::placeholder').color : cs.color);
    const fg = [foreground[0], foreground[1], foreground[2], foreground[3] * opacityOf(el)];
    const ratios = bgs.map((bg) => ratio(over(fg, bg), bg));
    const size = parseFloat(cs.fontSize);
    const bold = parseInt(cs.fontWeight, 10) >= 700;
    const large = size >= 24 || (bold && size >= 18.66);
    return {
      ratio: Math.min(...ratios), need: large ? 3 : 4.5, image, gradient,
      size, weight: cs.fontWeight, fg: cs.color,
      bg: [...new Set(bgs.map((c) => `rgb(${c.slice(0, 3).map(Math.round)})`))].slice(0, 4).join(' | '),
      text: sample,
    };
  }

  function run(options = {}) {
    const findings = [];
    const grouped = new Map();
    let checked = 0;
    let exempt = 0;
    for (const el of candidates()) {
      const isControl = el.matches(FORM_CONTROLS);
      let sample = isControl ? ((el.tagName === 'SELECT' ? el.selectedOptions[0]?.textContent : el.value) || '').trim() : ownText(el);
      const placeholder = isControl && !sample && el.getAttribute('placeholder');
      if (!sample && !placeholder) continue;
      if (el.closest('script, style, noscript, template, option')) continue;
      if (!visible(el) || el.closest('.visually-hidden, .visually-hidden-focusable, [aria-hidden="true"], [hidden]')) continue;
      if (el.closest(EXEMPT)) {
        exempt += 1;
        continue;
      }
      checked += 1;
      const kind = placeholder ? 'placeholder' : 'text';
      const result = check(el, (placeholder || sample).slice(0, 60), kind);
      let level = null;
      let note = '';
      if (result.image) {
        level = 'INFO';
        note = 'background image: check by eye';
      }
      if (result.ratio < result.need) {
        level = 'FAIL';
        note = result.image ? 'background image: ratio is for the colour behind it' : '';
        // The theme's own solid primary button: reported, not counted (design D7).
        if (el.closest('.btn-primary') && !el.closest('.btn-outline-primary')) {
          level = 'WARN';
          note = 'solid .btn-primary comes from the theme';
        }
      } else if (result.gradient && !result.image) {
        // The average passes; the darkest and lightest stops are only information.
        level = level || null;
      }
      if (!level) continue;
      const selector = describe(el);
      const key = [level, selector, result.fg, result.bg, result.ratio.toFixed(2)].join('|');
      if (grouped.has(key)) {
        grouped.get(key).count += 1;
        continue;
      }
      const finding = {
        check: 'contrast', level, selector, text: result.text, ratio: +result.ratio.toFixed(2), need: result.need,
        fg: result.fg, bg: result.bg, size: result.size, weight: result.weight, count: 1, note, kind,
        owner: el.closest('#nav-left') ? 'nav' : 'fleetops',
      };
      grouped.set(key, finding);
      findings.push(finding);
    }
    return { checked, exempt, findings };
  }

  window.__foContrast = { run, rgba, over, ratio, luminance, backgrounds };
})();
