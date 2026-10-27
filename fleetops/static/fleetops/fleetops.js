// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// FleetOps page helpers, loaded once by the base page. Every time here is EVE time (UTC), whatever the
// browser's time zone. Texts come from the JavaScript catalog (gettext, ngettext, pgettext, interpolate) that
// the base page loads before this script.
//
// window.FleetOps offers:
//   toast(message, level, {sticky, seconds, action: {label, href | onClick}})   a notification
//   confirm(text, {confirmLabel, title, danger}) -> Promise<boolean>              the confirmation dialog
//   eveClock(date), eveDate(date), eveStamp(date), roundedEveTime(now, minutes)   EVE time text
//   duration(minutes), relativeText(minutes), updateRelative()                    "1 h 05 min", "in 5 min"
//   copyText(text) -> Promise<boolean>                                            clipboard with a fallback
//   detection, roleLabel(code)                                                    fleet detection lines
// and these attributes work on every FleetOps page:
//   form[data-fo-confirm], button[data-fo-confirm]   ask before sending (see the submit handler below)
//   [data-relative-utc], [data-countdown-utc]        time left or gone, kept current
//   [data-since-utc]                                 time passed since then
//   .copy[data-id], .copy-btn[data-copy], [data-fo-copy]   copy buttons with "Copied" feedback
//   input[data-fo-filter="<list id>"]                hides non-matching [data-fo-filter-item] in that list
(() => {
  // The page may load this script twice (the base page and an older page include); the first copy wins.
  if (window.FleetOps && window.FleetOps.loaded) {
    return;
  }

  const pad = (number) => String(number).padStart(2, '0');

  // --- Time -------------------------------------------------------------------------------------------

  // "1 h 20 min" or "45 min".
  function duration(minutes) {
    const hours = Math.floor(minutes / 60);
    return hours
      ? interpolate(gettext('%(hours)s h %(minutes)s min'), { hours, minutes: pad(minutes % 60) }, true)
      : interpolate(gettext('%(minutes)s min'), { minutes }, true);
  }

  function eveClock(date) {
    return `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
  }

  function eveDate(date) {
    return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
  }

  // "2026-10-16 13:05 EVE": the one format for a single time on FleetOps pages.
  function eveStamp(date) {
    return `${eveDate(date)} ${eveClock(date)} EVE`;
  }

  // The current EVE time rounded up to five minutes, plus an offset; for quick fleet time buttons.
  function roundedEveTime(now, offsetMinutes) {
    const time = new Date(now.getTime());
    time.setUTCSeconds(0, 0);
    time.setUTCMinutes(Math.ceil(time.getUTCMinutes() / 5) * 5 + offsetMinutes);
    return { date: eveDate(time), clock: eveClock(time) };
  }

  // "in 30 min", "now" or "1 h 05 min ago" for a number of minutes from now (negative = in the past).
  function relativeText(minutes) {
    if (minutes > 0) {
      return interpolate(gettext('in %(duration)s'), { duration: duration(minutes) }, true);
    }
    if (minutes === 0) {
      return gettext('now');
    }
    return interpolate(gettext('%(duration)s ago'), { duration: duration(-minutes) }, true);
  }

  // A relative time next to an absolute one is only useful while it is near; beyond two days it stays empty.
  const RELATIVE_WINDOW_MINUTES = 48 * 60;

  // data-countdown-utc="2026-10-12T19:00:00Z" shows the time left until then (any distance);
  // data-relative-utc is the same for a time next to its date (two days each way); data-since-utc shows the
  // time that has passed since then, for running fleets.
  function updateRelative() {
    const now = Date.now();
    document.querySelectorAll('[data-countdown-utc], [data-relative-utc]').forEach((element) => {
      const near = element.dataset.relativeUtc !== undefined;
      const minutes = Math.round((Date.parse(near ? element.dataset.relativeUtc : element.dataset.countdownUtc) - now) / 60000);
      if (Number.isNaN(minutes) || (near && Math.abs(minutes) > RELATIVE_WINDOW_MINUTES)) {
        element.textContent = '';
      } else {
        element.textContent = relativeText(minutes);
      }
    });
    document.querySelectorAll('[data-since-utc]').forEach((element) => {
      const minutes = Math.floor((now - Date.parse(element.dataset.sinceUtc)) / 60000);
      element.textContent = Number.isNaN(minutes) ? '' : duration(Math.max(0, minutes));
    });
  }

  // The EVE clock in the brand of the navbar (HH:MM EVE), changed when the minute changes.
  function tickBrandClock() {
    const clock = document.getElementById('fo-eve-clock');
    if (!clock) {
      return;
    }
    clock.textContent = `${eveClock(new Date())} EVE`;
    window.setTimeout(tickBrandClock, 60000 - (Date.now() % 60000) + 50);
  }

  // The name of a fleet position from ESI's code, such as "squad_commander"; an unknown code is shown as it is.
  function roleLabel(code) {
    switch (code) {
      case 'fleet_commander':
        return pgettext('fleet position', 'Fleet Commander');
      case 'wing_commander':
        return pgettext('fleet position', 'Wing Commander');
      case 'squad_commander':
        return pgettext('fleet position', 'Squad Commander');
      case 'squad_member':
        return pgettext('fleet position', 'Squad Member');
      default:
        return code;
    }
  }

  // The lines under the character field of the start pages. `data` is the answer of the detect route.
  const detection = {
    select: () => gettext('Select a character to detect its current ESI fleet.'),
    checking: () => gettext('Checking fleet…'),
    failed: () => '⚠ ' + gettext('Fleet detection request failed.'),
    result(data) {
      if (!data.ok) {
        return '⚠ ' + data.message;
      }
      if (data.is_fleet_boss) {
        const text = interpolate(gettext('Fleet %(fleet_id)s — role: %(role)s — Fleet Boss'), { fleet_id: data.fleet_id, role: roleLabel(data.role) }, true);
        return '✓ ' + text;
      }
      const text = interpolate(gettext('Fleet %(fleet_id)s — %(message)s'), { fleet_id: data.fleet_id, message: data.message }, true);
      return '⚠ ' + text;
    },
  };

  // --- Notifications ----------------------------------------------------------------------------------

  const TOAST_TONES = { success: 'ok', ok: 'ok', info: 'info', warning: 'warn', warn: 'warn', danger: 'bad', error: 'bad', bad: 'bad' };
  const TOAST_SECONDS = 5;
  const MAX_TOASTS = 3;

  // A link is only followed when it stays on this site or goes to a web address.
  function safeHref(href) {
    try {
      const url = new URL(href, window.location.href);
      return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : '';
    } catch (error) {
      return '';
    }
  }

  // The fleet page has its own stack (#fleet-flash); every other page uses #fo-toasts of the base page.
  function toastStack() {
    let stack = document.getElementById('fleet-flash') || document.getElementById('fo-toasts');
    if (!stack) {
      stack = document.createElement('div');
      stack.id = 'fo-toasts';
      stack.className = 'fo-toasts';
      stack.setAttribute('aria-live', 'polite');
      document.body.appendChild(stack);
    }
    return stack;
  }

  function dismissToast(item) {
    window.clearTimeout(Number(item.dataset.foTimer || 0));
    item.remove();
  }

  // Shows a notification. `message` is text (or DOM nodes). Success and information go away after five
  // seconds; a warning or an error stays until it is closed, so the reason can be read. At most three show.
  function toast(message, level, options = {}) {
    const stack = toastStack();
    const tone = TOAST_TONES[level] || 'info';
    const item = document.createElement('div');
    item.className = `fo-toast fo-toast-${tone}`;
    item.setAttribute('role', tone === 'warn' || tone === 'bad' ? 'alert' : 'status');

    const body = document.createElement('div');
    body.className = 'fo-toast-body';
    const text = document.createElement('span');
    text.className = 'fo-toast-text';
    if (message instanceof Node) {
      text.appendChild(message);
    } else if (Array.isArray(message)) {
      message.forEach((node) => text.appendChild(node instanceof Node ? node : document.createTextNode(String(node))));
    } else {
      text.textContent = String(message);
    }
    body.appendChild(text);

    const action = options.action;
    if (action && action.label) {
      const href = action.href ? safeHref(action.href) : '';
      if (href) {
        const link = document.createElement('a');
        link.className = 'fo-toast-action';
        link.href = href;
        link.textContent = action.label;
        body.appendChild(link);
      } else if (typeof action.onClick === 'function') {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-sm btn-outline-secondary fo-toast-action';
        button.textContent = action.label;
        button.addEventListener('click', () => {
          action.onClick();
          dismissToast(item);
        });
        body.appendChild(button);
      }
    }
    item.appendChild(body);

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'fo-icon-btn fo-toast-close';
    close.setAttribute('aria-label', gettext('Close'));
    close.title = gettext('Close');
    const icon = document.createElement('i');
    icon.className = 'fa-solid fa-xmark';
    icon.setAttribute('aria-hidden', 'true');
    close.appendChild(icon);
    close.addEventListener('click', () => dismissToast(item));
    item.appendChild(close);

    stack.appendChild(item);
    const shown = stack.querySelectorAll('.fo-toast');
    for (let index = 0; index < shown.length - MAX_TOASTS; index += 1) {
      dismissToast(shown[index]);
    }
    if (!options.sticky && (tone === 'ok' || tone === 'info')) {
      item.dataset.foTimer = String(window.setTimeout(() => dismissToast(item), (options.seconds || TOAST_SECONDS) * 1000));
    }
    return item;
  }

  // Alliance Auth shows its own messages above the page; on FleetOps pages they become toasts too, so success
  // and failure look and behave the same everywhere. Without this script they stay as AA's alerts.
  function adoptServerMessages() {
    const page = document.querySelector('.fleetops');
    if (!page || !page.parentElement) {
      return;
    }
    Array.from(page.parentElement.children).forEach((alert) => {
      if (!alert.matches('.alert[role="alert"]') || !(alert.compareDocumentPosition(page) & Node.DOCUMENT_POSITION_FOLLOWING)) {
        return;
      }
      const found = /\balert-(success|info|warning|danger)\b/.exec(alert.className);
      const source = alert.querySelector(':scope > .ms-2') || alert;
      const nodes = Array.from(source.childNodes).filter((node) => node.nodeType === Node.TEXT_NODE ? node.textContent.trim() : true);
      toast(nodes, found ? found[1] : 'info');
      alert.remove();
    });
  }

  // Notices that the server kept for this page ({% fleetops_notices %}), such as "Marked paid." with an undo link.
  function showNotices() {
    const block = document.getElementById('fo-notices');
    if (!block) {
      return;
    }
    let notices = [];
    try {
      notices = JSON.parse(block.textContent);
    } catch (error) {
      notices = [];
    }
    block.remove();
    notices.forEach((notice) => {
      const action = notice.action_label && notice.action_url ? { label: notice.action_label, href: notice.action_url } : null;
      toast(notice.text, notice.level, { action });
    });
  }

  // --- Confirmation -----------------------------------------------------------------------------------

  const confirmState = { pending: null, bound: false };

  function settleConfirm(result) {
    const pending = confirmState.pending;
    if (pending) {
      confirmState.pending = null;
      pending.resolve(result);
    }
  }

  function bindConfirm(element, Modal) {
    if (confirmState.bound) {
      return;
    }
    confirmState.bound = true;
    element.querySelector('#fo-confirm-ok').addEventListener('click', () => {
      settleConfirm(true);
      Modal.getOrCreateInstance(element).hide();
    });
    element.addEventListener('shown.bs.modal', () => {
      const pending = confirmState.pending;
      const target = element.querySelector(pending && pending.danger ? '#fo-confirm-cancel' : '#fo-confirm-ok');
      if (target) {
        target.focus();
      }
    });
    element.addEventListener('hidden.bs.modal', () => settleConfirm(false));
  }

  // Asks a question in the shared dialog (#fo-confirm of the base page). The promise answers true on
  // "confirm" and false on cancel, the close button, Escape or a click outside. A dangerous question puts the
  // focus on Cancel. Without the dialog or Bootstrap's script the browser's own confirm() asks.
  function confirmDialog(text, options = {}) {
    const element = document.getElementById('fo-confirm');
    const Modal = window.bootstrap && window.bootstrap.Modal;
    const ok = element && element.querySelector('#fo-confirm-ok');
    const sentence = element && element.querySelector('#fo-confirm-text');
    if (!Modal || !ok || !sentence) {
      return Promise.resolve(window.confirm(String(text)));
    }
    settleConfirm(false);
    bindConfirm(element, Modal);
    const title = element.querySelector('#fo-confirm-title');
    for (const [node, key] of [[ok, 'foDefaultOk'], [title, 'foDefaultTitle']]) {
      if (node) {
        if (node.dataset[key] === undefined) {
          node.dataset[key] = node.textContent;
        }
        node.textContent = (node === ok ? options.confirmLabel : options.title) || node.dataset[key];
      }
    }
    sentence.textContent = String(text);
    const danger = Boolean(options.danger);
    ok.classList.toggle('btn-danger', danger);
    ok.classList.toggle('btn-primary', !danger);
    return new Promise((resolve) => {
      confirmState.pending = { resolve, danger };
      Modal.getOrCreateInstance(element).show();
    });
  }

  // Forms and buttons that carry data-fo-confirm="<question>" ask before they send. The question of the
  // button that was pressed wins over the one of the form, so "Approve" and "Reject" can share a form and
  // only Reject asks. The question is dangerous unless data-fo-confirm-danger="false"; data-fo-confirm-label
  // names the confirm button. After "confirm" the form is sent again with the same button, so its name and
  // value reach the server. The handler runs first (capture) and stops the first submit, so the other scripts
  // of the page (double-click guards, live updates) see only the confirmed one.
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    if (form.dataset.foConfirmed === '1') {
      delete form.dataset.foConfirmed;
      return;
    }
    const submitter = event.submitter || null;
    const source = submitter && submitter.dataset.foConfirm ? submitter : form;
    const question = source.dataset.foConfirm;
    if (!question) {
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    const options = { danger: source.dataset.foConfirmDanger !== 'false', confirmLabel: source.dataset.foConfirmLabel };
    confirmDialog(question, options).then((confirmed) => {
      if (!confirmed) {
        return;
      }
      form.dataset.foConfirmed = '1';
      try {
        if (typeof form.requestSubmit === 'function') {
          form.requestSubmit(submitter || undefined);
        } else {
          form.submit();
        }
      } finally {
        delete form.dataset.foConfirmed;
      }
    });
  }, true);

  // --- Copy buttons -----------------------------------------------------------------------------------

  const COPIED_MS = 1500;

  function fallbackCopy(text) {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.top = '-1000px';
    document.body.appendChild(area);
    area.select();
    let done = false;
    try {
      done = document.execCommand('copy');
    } catch (error) {
      done = false;
    }
    document.body.removeChild(area);
    return done;
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(() => true, () => fallbackCopy(text));
    }
    return Promise.resolve(fallbackCopy(text));
  }

  // The text a copy button copies: data-copy-text of the source element when it has one (a preview that shows
  // names but copies ids), else its value (a field) or its text.
  function copySource(button) {
    let source = null;
    if (button.dataset.foCopy) {
      source = document.querySelector(button.dataset.foCopy);
    } else if (button.dataset.copy) {
      source = document.querySelector(button.dataset.copy);
    } else if (button.dataset.id) {
      source = document.getElementById(button.dataset.id);
    }
    if (!source) {
      return null;
    }
    if (source.dataset.copyText !== undefined) {
      return source.dataset.copyText;
    }
    return typeof source.value === 'string' ? source.value : source.textContent;
  }

  function showCopied(button, ok) {
    if (button.foLabel === undefined) {
      button.foLabel = Array.from(button.childNodes);
    }
    button.replaceChildren(document.createTextNode(ok ? gettext('Copied') : gettext('Copy failed')));
    window.clearTimeout(Number(button.dataset.foTimer || 0));
    button.dataset.foTimer = String(window.setTimeout(() => {
      button.replaceChildren(...button.foLabel);
    }, COPIED_MS));
  }

  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-fo-copy], .copy-btn[data-copy], .copy[data-id]');
    if (!button) {
      return;
    }
    const text = copySource(button);
    if (text === null) {
      return;
    }
    copyText(text).then((ok) => showCopied(button, ok));
  });

  // --- Filter for long lists --------------------------------------------------------------------------

  // input[data-fo-filter="list-id"] hides the [data-fo-filter-item] elements of that list whose text (and
  // value of data-fo-filter-item) does not hold every word typed. An element with
  // data-fo-filter-empty="list-id" is shown while a filter hides everything.
  function applyFilter(input) {
    const list = document.getElementById(input.dataset.foFilter);
    if (!list) {
      return;
    }
    const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0;
    list.querySelectorAll('[data-fo-filter-item]').forEach((item) => {
      const text = `${item.dataset.foFilterItem || ''} ${item.textContent}`.toLowerCase();
      const match = words.every((word) => text.includes(word));
      item.hidden = !match;
      shown += match ? 1 : 0;
    });
    document.querySelectorAll('[data-fo-filter-empty]').forEach((empty) => {
      if (empty.dataset.foFilterEmpty === input.dataset.foFilter) {
        empty.hidden = shown > 0 || words.length === 0;
      }
    });
  }

  document.addEventListener('input', (event) => {
    if (event.target instanceof HTMLInputElement && event.target.dataset.foFilter) {
      applyFilter(event.target);
    }
  });
  // Enter in a filter field must not send the form around it.
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && event.target instanceof HTMLInputElement && event.target.dataset.foFilter) {
      event.preventDefault();
    }
  });

  // --- Page set-up ------------------------------------------------------------------------------------

  // Sticky bars sit under AA's fixed navbar, whose height depends on the theme.
  let measuring = false;
  function measureNavbar() {
    const navbar = document.querySelector('nav.navbar.fixed-top');
    if (navbar && navbar.offsetHeight) {
      document.documentElement.style.setProperty('--fo-sticky-top', `${navbar.offsetHeight}px`);
    }
    measuring = false;
  }

  // Themes that Alliance Auth does not know as dark (third-party hooks) are recognised by their page colour.
  function markDarkTheme() {
    for (const element of [document.body, document.documentElement]) {
      const found = /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?/.exec(getComputedStyle(element).backgroundColor);
      if (found && (found[4] === undefined || Number(found[4]) > 0)) {
        const [red, green, blue] = [found[1], found[2], found[3]].map((value) => {
          const channel = Number(value) / 255;
          return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
        });
        document.documentElement.toggleAttribute('data-fo-dark', 0.2126 * red + 0.7152 * green + 0.0722 * blue < 0.2);
        return;
      }
    }
  }

  function start() {
    measureNavbar();
    markDarkTheme();
    adoptServerMessages();
    showNotices();
    updateRelative();
    tickBrandClock();
  }

  window.addEventListener('resize', () => {
    if (!measuring) {
      measuring = true;
      window.requestAnimationFrame(measureNavbar);
    }
  });
  new MutationObserver(markDarkTheme).observe(document.documentElement, { attributes: true, attributeFilter: ['data-bs-theme', 'data-theme', 'class'] });
  setInterval(updateRelative, 30000);
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  window.FleetOps = {
    loaded: true,
    duration,
    eveClock,
    eveDate,
    eveStamp,
    roundedEveTime,
    relativeText,
    updateRelative,
    updateCountdowns: updateRelative,
    roleLabel,
    detection,
    toast,
    confirm: confirmDialog,
    copyText,
  };
})();
