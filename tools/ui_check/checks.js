// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// In-page checks of the rendered FleetOps interface. Injected into the page by run.js; exposes
// window.__foChecks.run(options) -> {findings}. options: {width, forbiddenClassPatterns: [regex source, ...]}.
//
// Exempt everywhere: [data-ui-check-skip], .fo-ingame and .fo-discord (they imitate other products).
// Checks: page and content overflow, clipped text, touch targets, icon-only controls without a name, forbidden
// classes inserted by scripts, the type scale, body and help text size, tabular numbers, image frames and
// more than one solid primary button in a region. Contrast has its own file (contrast.js); focus, motion and
// console errors need the browser and are done by run.js.
(() => {
  'use strict';

  const EXEMPT = '[data-ui-check-skip], .fo-ingame, .fo-discord';
  const ROOT_SELECTOR = '.fleetops, #fo-toasts, #fo-confirm';

  function describe(el) {
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

  function visible(el) {
    if (typeof el.checkVisibility === 'function' && !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  }

  function inScope() {
    // Every element of the FleetOps container, toasts, confirm dialog and open modals, without the exempt ones.
    const found = new Set();
    for (const selector of [ROOT_SELECTOR, '.modal.show']) {
      for (const root of document.querySelectorAll(selector)) {
        found.add(root);
        root.querySelectorAll('*').forEach((el) => found.add(el));
      }
    }
    return [...found].filter((el) => !el.closest(EXEMPT) && !el.closest('script, style, template, noscript'));
  }

  function run(options = {}) {
    const width = options.width || window.innerWidth;
    const findings = [];
    const grouped = new Map();

    function add(check, level, el, message, detail = {}) {
      const selector = el ? describe(el) : '';
      const key = [check, level, selector, message].join('|');
      if (grouped.has(key)) {
        grouped.get(key).count += 1;
        return;
      }
      const finding = {
        check, level, selector, message, count: 1,
        text: el ? (ownText(el) || el.getAttribute('aria-label') || el.getAttribute('title') || '').slice(0, 60) : '',
        ...detail,
      };
      grouped.set(key, finding);
      findings.push(finding);
    }

    const elements = inScope().filter(visible);
    // Visually hidden text is cut to one pixel on purpose; it only matters for control names.
    const shown = elements.filter((el) => !el.closest('.visually-hidden, .visually-hidden-focusable, .sr-only'));
    const hasContainer = !!document.querySelector('.fleetops');

    // ---- overflow ---------------------------------------------------------------------------------------
    const doc = document.documentElement;
    if (doc.scrollWidth > doc.clientWidth + 1) {
      add('overflow', 'FAIL', document.querySelector('.fleetops') || document.body,
        `the page is wider than the window (${doc.scrollWidth}px content, ${doc.clientWidth}px window)`,
        { owner: 'page' });
    }
    const container = document.querySelector('.fleetops');
    if (container) {
      // Alliance Auth puts the page in its own scrolling area; content wider than that area scrolls sideways.
      let area = container.parentElement;
      while (area && area !== document.body && !/(auto|scroll)/.test(getComputedStyle(area).overflowX)) area = area.parentElement;
      if (area && area !== document.body && area.scrollWidth > area.clientWidth + 1) {
        const limit = area.getBoundingClientRect().left + area.clientWidth;
        const wide = elements.filter((el) => {
          if (el.getBoundingClientRect().right <= limit + 1) return false;
          for (let n = el.parentElement; n && n !== area; n = n.parentElement) {
            if (/(auto|scroll|hidden|clip)/.test(getComputedStyle(n).overflowX)) return false;
          }
          return true;
        });
        const culprits = wide.filter((el) => !wide.some((other) => other !== el && el.contains(other)));
        for (const el of (culprits.length ? culprits : [container]).slice(0, 5)) {
          add('overflow', 'FAIL', el,
            `content is wider than the page area (${area.scrollWidth}px, area ${area.clientWidth}px)`, { owner: 'fleetops' });
        }
      }
    }

    // ---- clipped text -----------------------------------------------------------------------------------
    for (const el of shown) {
      if (!ownText(el)) continue;
      const cs = getComputedStyle(el);
      const clipsX = cs.textOverflow === 'ellipsis' || /(hidden|clip)/.test(cs.overflowX);
      if (clipsX && el.scrollWidth > el.clientWidth + 1) {
        add('clipped', 'FAIL', el, `text is cut off (${el.scrollWidth}px of text in ${el.clientWidth}px)`);
      } else if ((cs.webkitLineClamp && cs.webkitLineClamp !== 'none') && el.scrollHeight > el.clientHeight + 1) {
        add('clipped', 'FAIL', el, 'text is cut off by a line limit');
      }
    }

    // ---- touch targets ----------------------------------------------------------------------------------
    if (width <= 992) {
      const level = width <= 576 ? 'FAIL' : 'WARN';
      const selector = '.btn, .page-link, .nav-link, .dropdown-item, [role=button], summary, .fo-icon-btn, .fleet-toggle, input[type=checkbox], input[type=radio], select';
      for (const el of elements.filter((e) => e.matches(selector))) {
        if (el.closest('#nav-left, .navbar')) continue;
        if (el.matches(':disabled, [aria-disabled="true"]')) continue;
        let box = el.getBoundingClientRect();
        if (el.matches('input[type=checkbox], input[type=radio]')) {
          const label = el.closest('label') || (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`));
          if (label) {
            const lb = label.getBoundingClientRect();
            box = { width: Math.max(box.width, lb.width), height: Math.max(box.height, lb.height) };
          }
        }
        if (box.width < 43.5 || box.height < 43.5) {
          add('touch', level, el, `touch target is ${Math.round(box.width)}x${Math.round(box.height)}px, at least 44x44 is needed`);
        }
      }
    }

    // ---- menu: every top-level entry is on screen and on one line (below the width where it collapses) ------
    if (width >= 992) {
      for (const link of document.querySelectorAll('#nav-left .navbar-nav > .nav-item > .nav-link, .navbar .navbar-nav > .nav-item > .nav-link')) {
        const box = link.getBoundingClientRect();
        if (!box.width) continue;
        if (box.right > window.innerWidth + 1 || box.left < -1) {
          add('menu', 'FAIL', link, 'a menu entry is outside the window, so it cannot be reached');
        } else if (box.height > 2.6 * parseFloat(getComputedStyle(link).lineHeight || 24)) {
          add('menu', 'FAIL', link, 'a menu entry wraps onto several lines');
        }
      }
    }

    // ---- controls without a name ------------------------------------------------------------------------
    for (const el of elements.filter((e) => e.matches('button, a[href], [role=button], input[type=button], input[type=submit]'))) {
      if (el.matches('[aria-hidden="true"]')) continue;
      const visibleText = [...el.querySelectorAll('*'), el].some((n) => {
        if (n.closest('[aria-hidden="true"], .visually-hidden')) return false;
        return [...n.childNodes].some((c) => c.nodeType === 3 && c.textContent.trim());
      });
      const hiddenText = !!el.querySelector('.visually-hidden, .visually-hidden-focusable, .sr-only');
      const label = el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.getAttribute('value') || '';
      const title = el.getAttribute('title') || '';
      const hasImageName = [...el.querySelectorAll('img')].some((i) => i.getAttribute('alt'));
      if (!visibleText && !hiddenText && !label && !title && !hasImageName) {
        add('names', 'FAIL', el, 'a control without any name (no text, aria-label or title)');
      } else if (!visibleText && !hiddenText && !hasImageName && !title) {
        add('names', 'FAIL', el, 'an icon-only control without a title (tooltip)');
      }
    }

    // ---- classes inserted at run time ------------------------------------------------------------------
    const patterns = (options.forbiddenClassPatterns || []).map((p) => new RegExp(`^(?:${p})$`));
    if (patterns.length) {
      for (const el of elements) {
        for (const token of el.classList) {
          if (patterns.some((p) => p.test(token))) add('classes', 'FAIL', el, `forbidden class "${token}" in the page`);
        }
      }
    }

    if (hasContainer) {
      // ---- type scale (D20) -----------------------------------------------------------------------------
      const h2 = width >= 576 ? 28 : 24;
      const near = (a, b) => Math.abs(a - b) <= 0.6;
      for (const el of shown) {
        const text = ownText(el);
        if (!text || el.matches('input, select, textarea')) continue;
        const cs = getComputedStyle(el);
        const size = parseFloat(cs.fontSize);
        const weight = parseInt(cs.fontWeight, 10);
        if (size < 12) add('type', 'FAIL', el, `text is ${size}px, nothing may be below 12px`);
        if (weight < 400) add('type', 'FAIL', el, `text weight is ${weight}, nothing may be lighter than 400`);
        if (el.matches('.fleetops h2') && (!near(size, h2) || weight !== 600)) {
          add('type', 'FAIL', el, `h2 is ${size}px / ${weight}, it must be ${h2}px / 600`);
        }
        if (el.matches('.card-header, .card-title, .modal-title') && (!near(size, 17) || weight !== 600)) {
          add('type', 'FAIL', el, `a card or dialog title is ${size}px / ${weight}, it must be 17px / 600`);
        }
        if (el.matches('.fo-metric-value') && (size < 35.5 || size > 44.5 || weight !== 500)) {
          add('type', 'FAIL', el, `a metric value is ${size}px / ${weight}, it must be 36-44px / 500`);
        }
        // Body and help text follow the theme's size.
        if (el.matches('.fleetops p, .fleetops td, .fleetops th, .fleetops li') && !el.matches('.form-text') && (size < 15 || size > 16.01)) {
          add('type-body', 'WARN', el, `paragraph or table text is ${size}px, 15-16px is expected`);
        }
        if (el.matches('.form-text') && (size < 12.99 || size > 14.01)) {
          add('type-body', 'WARN', el, `help text is ${size}px, 13-14px is expected`);
        }
        // ---- tabular numbers (D21) --------------------------------------------------------------------
        if (/\d/.test(text) && !/tabular-nums/.test(cs.fontVariantNumeric)) {
          add('numbers', 'FAIL', el, 'a number without tabular figures (font-variant-numeric: tabular-nums)');
        }
      }

      // ---- images (UD6.1) -------------------------------------------------------------------------------
      for (const img of shown.filter((e) => e.tagName === 'IMG')) {
        const cs = getComputedStyle(img);
        if (parseFloat(cs.borderTopWidth) < 0.99 || parseFloat(cs.borderTopLeftRadius) <= 0) {
          add('images', 'FAIL', img, `an image without its frame (border ${cs.borderTopWidth}, radius ${cs.borderTopLeftRadius})`);
        }
      }

      // ---- more than one solid primary button in a region (UD4.3) ----------------------------------------
      const regions = new Map();
      for (const button of elements.filter((e) => e.matches('.btn-primary'))) {
        const region = button.closest('.card, form, .modal, .fo-actionbar, .fo-commandbar');
        if (!region) continue;
        if (!regions.has(region)) regions.set(region, []);
        regions.get(region).push(button);
      }
      for (const [region, buttons] of regions) {
        if (buttons.length > 1) add('primary', 'WARN', region, `${buttons.length} solid primary buttons in one region; keep one`);
      }
    }
    return { findings };
  }

  window.__foChecks = { run };
})();
