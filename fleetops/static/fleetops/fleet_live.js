// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// Fleet tab: state polling, fragment swaps and the shared action handler (window.FleetOpsFleet).
//
// The server renders every piece of HTML (escaped there); this script only swaps those fragments
// and writes plain text with textContent. Every time shown is EVE time (UTC).
//
// window.FleetOpsFleet:
//   root                      the #fleet-root element
//   applyState(state)         apply a state payload (poller answer or the "state" of an action answer)
//   request(url, formData)    POST with the CSRF token; resolves to the JSON answer and applies its state
//   getJSON(url)              GET a JSON answer of a Fleet tab route
//   flash(message, level)     show a message above the tree (level: success, info, warning, error)
//   isBusy() / setBusy(key, on)  swaps wait while something is busy (drag, open menu or modal, typing)
//   onRender(fn)              fn(state) runs after every swap; returns a function that removes it
//   selected()                the checked members as a Set of character ids (strings)
//   pollSoon(ms)              ask for the state again in ms milliseconds
//   confirm(text)             the shared confirmation dialog; resolves to true or false
//   submit(form, submitter)   send a form[data-fleet-action] like the delegated handler does
// After an action answer, a "fleetops:answer" event with detail {form, answer} is dispatched on document.
(() => {
  const root = document.getElementById('fleet-root');
  if (!root || window.FleetOpsFleet) {
    return;
  }

  // Tabs whose content the state payload replaces; the others show "Updated — reload page".
  const LIVE_TABS = ['#fleet', '#members', '#attendance'];
  const FRAGMENTS = {
    status_html: 'fleet-status',
    tree_html: 'fleet-tree',
    composition_html: 'fleet-composition',
    changes_html: 'fleet-changes',
    members_html: 'members-fragment',
    pap_html: 'pap-preview-fragment',
    controls_html: 'fleet-control-fragment',
  };
  const LEVELS = { success: 'success', info: 'info', warning: 'warning', error: 'danger' };
  const REQUEST_TIMEOUT = 45000;
  const MAX_DELAY_SECONDS = 60;
  const FLASH_SECONDS = 8;
  const FLASHES_SHOWN = 3;
  const SESSION_ENDED = gettext('Your session has expired — reload the page.');
  const STILL_WAITING = gettext('Still waiting for ESI — the result will show after the next refresh.');
  const NOT_SENT = gettext('The request did not reach the server. Check your connection and try again.');

  const operation = root.dataset.operation || '';
  const stateUrl = root.dataset.stateUrl;
  const canManage = root.dataset.canManage === '1';
  const pollSeconds = Math.max(5, parseInt(root.dataset.pollSeconds, 10) || 10);
  const pageStatus = root.dataset.status;
  // Only an active fleet changes; an ending one reloads the page and a closed one shows its composition.
  const polling = pageStatus === 'active' && Boolean(stateUrl);
  const collapsedKey = `fleetops:collapsed:${operation}`;
  const liveKey = 'fleetops:fleet-live';

  const storage = {
    get(key) {
      try {
        return window.localStorage.getItem(key);
      } catch (error) {
        return null;
      }
    },
    set(key, value) {
      try {
        window.localStorage.setItem(key, value);
      } catch (error) {
        // Private windows may refuse storage; the page works without it.
      }
    },
  };

  class FleetError extends Error {
    constructor(message, level) {
      super(message);
      this.level = level || 'error';
    }
  }

  let version = root.dataset.version || '';
  let offset = 0; // server clock minus browser clock, in milliseconds
  let lastRead = null;
  let pending = null;
  let failures = 0;
  let stopped = false;
  let inFlight = false;
  let again = false;
  let timer = null;
  let activeFunction = null;
  const busy = new Set();
  const renderCallbacks = [];
  const collapsed = new Set(readCollapsed());

  function readCollapsed() {
    try {
      const value = JSON.parse(storage.get(collapsedKey) || '[]');
      return Array.isArray(value) ? value.map(String) : [];
    } catch (error) {
      return [];
    }
  }

  function saveCollapsed() {
    storage.set(collapsedKey, JSON.stringify(Array.from(collapsed)));
  }

  // Times

  const pad = (number) => String(number).padStart(2, '0');

  function clock(milliseconds) {
    const date = new Date(milliseconds);
    return `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
  }

  function ago(seconds) {
    if (seconds < 120) {
      return interpolate(gettext('%(count)s s ago'), { count: seconds }, true);
    }
    const minutes = Math.floor(seconds / 60);
    return minutes < 120
      ? interpolate(gettext('%(count)s min ago'), { count: minutes }, true)
      : interpolate(gettext('%(count)s h ago'), { count: Math.floor(minutes / 60) }, true);
  }

  function parseTime(value) {
    const time = value ? Date.parse(value) : NaN;
    return Number.isNaN(time) ? null : time;
  }

  function setText(id, text) {
    const element = document.getElementById(id);
    if (element && element.textContent !== text) {
      element.textContent = text;
    }
  }

  function tick() {
    const now = Date.now() + offset;
    setText('fleet-eve-clock', clock(now));
    if (lastRead !== null) {
      setText('fleet-last-read-age', `(${ago(Math.max(0, Math.round((now - lastRead) / 1000)))})`);
    }
  }

  function noteTimes(state) {
    const server = parseTime(state.server_time);
    if (server !== null) {
      offset = server - Date.now();
      setText('fleet-updated-at', clock(server));
    }
    if (Object.prototype.hasOwnProperty.call(state, 'last_esi_update')) {
      lastRead = parseTime(state.last_esi_update);
      if (lastRead !== null) {
        setText('fleet-last-read', clock(lastRead));
      }
    }
    const stale = document.getElementById('fleet-stale');
    if (stale && Object.prototype.hasOwnProperty.call(state, 'stale')) {
      stale.classList.toggle('d-none', !state.stale);
    }
    tick();
  }

  // Tabs and hints

  function shownTab() {
    const link = document.querySelector('#operation-tabs .nav-link.active');
    return link ? link.dataset.bsTarget : '#fleet';
  }

  function liveTabShown() {
    return LIVE_TABS.includes(shownTab());
  }

  function showReloadHint(text) {
    const hint = document.getElementById('fleet-reload-hint');
    if (!hint) {
      return;
    }
    if (text) {
      hint.textContent = text;
    }
    hint.classList.remove('d-none');
  }

  // Busy state: a swap would close a menu, a dialog or an editor, or drop a drag.

  function insideFragment(element) {
    return Object.values(FRAGMENTS).some((id) => {
      const fragment = document.getElementById(id);
      return fragment !== null && fragment.contains(element);
    });
  }

  function isBusy() {
    if (busy.size) {
      return true;
    }
    if (document.querySelector('.dropdown-menu.show, .modal.show')) {
      return true;
    }
    const active = document.activeElement;
    return Boolean(
      active &&
        active !== document.body &&
        active.matches('textarea, select, input:not([type="checkbox"]):not([type="radio"]):not([type="submit"]):not([type="button"]):not([type="hidden"])') &&
        insideFragment(active),
    );
  }

  function setBusy(key, on) {
    if (on) {
      busy.add(key);
    } else {
      busy.delete(key);
      applyPending();
    }
  }

  function applyPending() {
    if (pending && !isBusy()) {
      const state = pending;
      pending = null;
      render(state);
    }
  }

  // Tree state that survives a swap: collapsed nodes, selected members, the highlighted function.

  function selected() {
    return new Set(Array.from(root.querySelectorAll('input.fleet-select:checked'), (box) => box.value));
  }

  function applyCollapsed() {
    root.querySelectorAll('[data-fleet-toggle]').forEach((button) => {
      const node = button.closest('.fleet-node');
      const shut = collapsed.has(button.dataset.fleetToggle);
      if (node) {
        node.classList.toggle('is-collapsed', shut);
      }
      button.setAttribute('aria-expanded', shut ? 'false' : 'true');
    });
  }

  function applyHighlight() {
    let found = false;
    root.querySelectorAll('[data-function-filter]').forEach((row) => {
      const on = activeFunction !== null && row.dataset.functionFilter === activeFunction;
      found = found || on;
      row.classList.toggle('active', on);
      if (row.getAttribute('role') === 'button') {
        row.setAttribute('aria-pressed', on ? 'true' : 'false');
      }
    });
    if (!found) {
      activeFunction = null;
    }
    root.querySelectorAll('.fleet-member').forEach((member) => {
      const match = activeFunction !== null && member.dataset.functionId === activeFunction;
      member.classList.toggle('fleet-highlight', match);
      member.classList.toggle('fleet-dim', activeFunction !== null && !match);
    });
  }

  function applyLiveToggle() {
    const toggle = document.getElementById('fleet-live-toggle');
    const saved = storage.get(liveKey);
    if (toggle && !toggle.disabled && saved !== null) {
      toggle.checked = saved === '1';
    }
  }

  function restore(selection) {
    applyCollapsed();
    root.querySelectorAll('input.fleet-select').forEach((box) => {
      box.checked = selection.has(box.value);
    });
    applyHighlight();
    applyLiveToggle();
  }

  // Keyboard focus inside a swapped fragment moves to the same control in the new HTML: the control
  // is found again by its member, wing or squad and its place among that one's controls.
  const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])';

  function anchorSelector(anchor) {
    if (anchor.id) {
      return `#${CSS.escape(anchor.id)}`;
    }
    const names = anchor.hasAttribute('data-character-id')
      ? ['data-character-id']
      : ['data-node', 'data-drop-role', 'data-wing-id', 'data-squad-id'];
    const attributes = names
      .filter((name) => anchor.hasAttribute(name))
      .map((name) => `[${name}="${CSS.escape(anchor.getAttribute(name))}"]`)
      .join('');
    const kind = anchor.classList.length ? `.${CSS.escape(anchor.classList[0])}` : '';
    return `${anchor.tagName.toLowerCase()}${kind}${attributes}`;
  }

  function focusSpot(state) {
    const active = document.activeElement;
    if (!active || active === document.body || !window.CSS || !CSS.escape) {
      return null;
    }
    const entry = Object.entries(FRAGMENTS).find(([key, id]) => {
      const fragment = document.getElementById(id);
      return typeof state[key] === 'string' && fragment !== null && fragment !== active && fragment.contains(active);
    });
    if (!entry) {
      return null;
    }
    const fragment = document.getElementById(entry[1]);
    const anchor = active.closest('[data-character-id], .fleet-node-head, [id]');
    const scope = anchor && anchor !== fragment && fragment.contains(anchor) ? anchor : fragment;
    return {
      id: entry[1],
      selector: scope === fragment ? '' : anchorSelector(scope),
      index: Array.from(scope.querySelectorAll(FOCUSABLE)).indexOf(active),
    };
  }

  function restoreFocus(spot) {
    if (!spot || (document.activeElement && document.activeElement !== document.body)) {
      return;
    }
    const fragment = document.getElementById(spot.id);
    if (!fragment) {
      return;
    }
    let scope = fragment;
    if (spot.selector) {
      try {
        scope = fragment.querySelector(spot.selector);
      } catch (error) {
        scope = null;
      }
    }
    let target = scope && spot.index >= 0 ? scope.querySelectorAll(FOCUSABLE)[spot.index] : null;
    if (!target) {
      // The control is gone: keep the keyboard in the part of the page it was in.
      if (!fragment.hasAttribute('tabindex')) {
        fragment.setAttribute('tabindex', '-1');
      }
      target = fragment;
    }
    target.focus({ preventScroll: true });
  }

  // Controls outside the swapped fragments (toolbar, selection bar) follow the write state.
  function applyWriteState(write) {
    root.dataset.canWrite = write.allowed ? '1' : '0';
    root.querySelectorAll('[data-needs-write]').forEach((element) => {
      if (!('fleetTitle' in element.dataset)) {
        element.dataset.fleetTitle = element.getAttribute('title') || '';
      }
      element.disabled = !write.allowed;
      const title = write.allowed ? element.dataset.fleetTitle : write.reason || element.dataset.fleetTitle;
      if (title) {
        element.setAttribute('title', title);
      } else {
        element.removeAttribute('title');
      }
    });
  }

  // States

  function render(state) {
    const selection = selected();
    const spot = focusSpot(state);
    Object.entries(FRAGMENTS).forEach(([key, id]) => {
      const element = document.getElementById(id);
      if (element && typeof state[key] === 'string') {
        element.innerHTML = state[key];
      }
    });
    if (state.version) {
      version = state.version;
      root.dataset.version = version;
    }
    if (state.write) {
      applyWriteState(state.write);
    }
    if (typeof state.member_count === 'number') {
      setText('stat-characters', String(state.member_count));
    }
    if (state.stat_last_esi) {
      setText('stat-last-esi', state.stat_last_esi);
    }
    restore(selection);
    restoreFocus(spot);
    noteTimes(state);
    if (failures) {
      showPollProblem();
    }
    renderCallbacks.slice().forEach((callback) => {
      try {
        callback(state);
      } catch (error) {
        window.setTimeout(() => {
          throw error;
        });
      }
    });
    if (!liveTabShown()) {
      showReloadHint();
    }
    if (state.status && state.status !== pageStatus) {
      statusChanged();
    }
  }

  function applyState(state) {
    if (!state || typeof state !== 'object') {
      return;
    }
    noteTimes(state);
    if (state.unchanged) {
      return;
    }
    if (isBusy()) {
      pending = state;
      return;
    }
    pending = null;
    render(state);
  }

  function statusChanged() {
    // The fleet is ending or closed: the page shows that state after a reload.
    stop();
    if (!isBusy() && liveTabShown()) {
      window.location.reload();
      return;
    }
    showReloadHint();
  }

  // Polling

  function liveWanted() {
    const toggle = document.getElementById('fleet-live-toggle');
    return canManage && Boolean(toggle) && toggle.checked && !toggle.disabled && liveTabShown();
  }

  function nextDelay() {
    return Math.min(MAX_DELAY_SECONDS, pollSeconds * 2 ** failures) * 1000;
  }

  function schedule(milliseconds) {
    window.clearTimeout(timer);
    timer = null;
    if (stopped || !polling) {
      return;
    }
    timer = window.setTimeout(poll, milliseconds === undefined ? nextDelay() : milliseconds);
  }

  function stop() {
    stopped = true;
    window.clearTimeout(timer);
    timer = null;
  }

  function isJson(response) {
    return (response.headers.get('Content-Type') || '').includes('application/json');
  }

  function showPollProblem() {
    const element = document.getElementById('fleet-poll-problem');
    if (!element) {
      return;
    }
    element.textContent = interpolate(gettext('No connection to the server — trying again in %(seconds)s s.'), { seconds: Math.round(nextDelay() / 1000) }, true);
    element.classList.remove('d-none');
  }

  function clearPollProblem() {
    const element = document.getElementById('fleet-poll-problem');
    if (element) {
      element.textContent = '';
      element.classList.add('d-none');
    }
  }

  function sessionEnded() {
    stop();
    pending = null;
    const alert = flash(SESSION_ENDED, 'warning', { sticky: true });
    if (alert) {
      const link = document.createElement('a');
      link.href = '';
      link.className = 'alert-link ms-1';
      link.textContent = gettext('Reload');
      link.addEventListener('click', (event) => {
        event.preventDefault();
        window.location.reload();
      });
      alert.insertBefore(link, alert.lastChild);
    }
    showReloadHint(gettext('Session expired — reload page'));
  }

  async function poll() {
    if (stopped || !polling) {
      return;
    }
    if (document.visibilityState === 'hidden') {
      // Polling resumes when the page is shown again.
      window.clearTimeout(timer);
      timer = null;
      return;
    }
    if (inFlight) {
      again = true;
      return;
    }
    inFlight = true;
    window.clearTimeout(timer);
    timer = null;
    const url = new URL(stateUrl, window.location.href);
    url.searchParams.set('v', version);
    url.searchParams.set('live', liveWanted() ? '1' : '0');
    try {
      const response = await window.fetch(url.toString(), {
        headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (response.status >= 500) {
        throw new FleetError(`HTTP ${response.status}`);
      }
      if (response.redirected || !response.ok || !isJson(response)) {
        // A login page, or the fleet is no longer visible to this user.
        sessionEnded();
        return;
      }
      const state = await response.json();
      failures = 0;
      clearPollProblem();
      applyState(state);
    } catch (error) {
      failures += 1;
      showPollProblem();
    } finally {
      inFlight = false;
      if (again) {
        again = false;
        schedule(500);
      } else {
        schedule();
      }
    }
  }

  function pollSoon(milliseconds) {
    if (!polling || stopped) {
      return;
    }
    window.setTimeout(poll, Math.max(0, milliseconds || 0));
  }

  // Requests and messages

  function csrfToken() {
    const input = root.querySelector('input[name="csrfmiddlewaretoken"]');
    return input ? input.value : '';
  }

  async function answerOf(response) {
    if (!isJson(response)) {
      if (response.redirected) {
        sessionEnded();
        throw new FleetError(SESSION_ENDED, 'warning');
      }
      if (response.status === 401 || response.status === 403) {
        throw new FleetError(gettext('This was refused. Your session may have expired — reload the page.'), 'error');
      }
      throw new FleetError(interpolate(gettext('The server answered with HTTP %(status)s. Try again in a moment.'), { status: response.status }, true), 'error');
    }
    const answer = await response.json();
    if (answer && answer.state) {
      applyState(answer.state);
    }
    return answer;
  }

  async function send(url, options) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
    try {
      return await window.fetch(url, { credentials: 'same-origin', signal: controller.signal, ...options });
    } catch (error) {
      throw new FleetError(error && error.name === 'AbortError' ? STILL_WAITING : NOT_SENT, 'warning');
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function request(url, formData) {
    const response = await send(url, {
      method: 'POST',
      body: formData || new FormData(),
      headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': csrfToken() },
    });
    return answerOf(response);
  }

  async function getJSON(url) {
    const response = await send(url, {
      headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      cache: 'no-store',
    });
    return answerOf(response);
  }

  function flash(message, level, options) {
    const area = document.getElementById('fleet-flash');
    if (!area || !message) {
      return null;
    }
    const kind = LEVELS[level] ? level : 'info';
    const alert = document.createElement('div');
    alert.className = `alert alert-${LEVELS[kind]} alert-dismissible py-2 mb-2`;
    alert.setAttribute('role', kind === 'error' || kind === 'warning' ? 'alert' : 'status');
    const text = document.createElement('span');
    text.textContent = message;
    alert.appendChild(text);
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close';
    close.setAttribute('aria-label', gettext('Close'));
    close.addEventListener('click', () => alert.remove());
    alert.appendChild(close);
    area.prepend(alert);
    while (area.children.length > FLASHES_SHOWN) {
      area.lastElementChild.remove();
    }
    if (!(options && options.sticky) && (kind === 'success' || kind === 'info')) {
      window.setTimeout(() => alert.remove(), FLASH_SECONDS * 1000);
    }
    return alert;
  }

  function confirmAction(text) {
    return new Promise((resolve) => {
      const modal = document.getElementById('fleet-confirm');
      const ok = document.getElementById('fleet-confirm-ok');
      if (!window.bootstrap || !modal || !ok) {
        // A theme without Bootstrap's scripts.
        resolve(window.confirm(text));
        return;
      }
      setText('fleet-confirm-text', text);
      const dialog = window.bootstrap.Modal.getOrCreateInstance(modal);
      let confirmed = false;
      const onOk = () => {
        confirmed = true;
        dialog.hide();
      };
      const onHidden = () => {
        ok.removeEventListener('click', onOk);
        modal.removeEventListener('hidden.bs.modal', onHidden);
        resolve(confirmed);
      };
      ok.addEventListener('click', onOk);
      modal.addEventListener('hidden.bs.modal', onHidden);
      dialog.show();
    });
  }

  function setButtonBusy(button, on) {
    if (!button) {
      return;
    }
    button.disabled = on;
    const spinner = button.querySelector('.fleet-spinner');
    if (on && !spinner) {
      const icon = document.createElement('span');
      icon.className = 'spinner-border spinner-border-sm ms-1 fleet-spinner';
      icon.setAttribute('aria-hidden', 'true');
      button.appendChild(icon);
    } else if (!on && spinner) {
      spinner.remove();
    }
  }

  // Alliance Auth marks a form "is-submitting" when it is sent and refuses to send it again. Fleet
  // forms are sent by script and stay on the page, so the mark is cleared again.
  function sendable(form) {
    form.classList.remove('is-submitting');
  }

  async function submitAction(form, submitter) {
    if (form.dataset.fleetSending) {
      return;
    }
    // The fields as they are now: other scripts may change or hide the form while a dialog is open.
    const data = new FormData(form);
    if (submitter && submitter.name) {
      data.append(submitter.name, submitter.value);
    }
    const button =
      submitter && form.contains(submitter)
        ? submitter
        : form.querySelector('button:not([type="button"]), input[type="submit"]');
    if (form.dataset.confirm && !(await confirmAction(form.dataset.confirm))) {
      sendable(form);
      return;
    }
    form.dataset.fleetSending = '1';
    setButtonBusy(button, true);
    if (form.contains(document.activeElement)) {
      // A focused field would hold back the swap that shows the result.
      document.activeElement.blur();
    }
    try {
      const answer = await request(form.action, data);
      if (answer && answer.message) {
        flash(answer.message, answer.level || (answer.ok ? 'success' : 'error'));
      }
      document.dispatchEvent(new CustomEvent('fleetops:answer', { detail: { form, answer } }));
    } catch (error) {
      flash(error.message, error.level || 'error');
    } finally {
      delete form.dataset.fleetSending;
      sendable(form);
      setButtonBusy(button, false);
      pollSoon(2000);
      pollSoon(8000);
    }
  }

  // Events

  function isFleetForm(form) {
    return form instanceof HTMLFormElement && form.matches('form[data-fleet-action]') && root.contains(form);
  }

  // Before Alliance Auth's own listener on the form: a mark left from an earlier send must not
  // stop this one (the handler below has its own guard against double sends).
  document.addEventListener(
    'submit',
    (event) => {
      if (isFleetForm(event.target)) {
        sendable(event.target);
      }
    },
    true,
  );

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!isFleetForm(form)) {
      return;
    }
    if (event.defaultPrevented) {
      // Another Fleet tab script handles this form itself.
      return;
    }
    event.preventDefault();
    submitAction(form, event.submitter);
  });

  root.addEventListener('click', (event) => {
    const toggle = event.target.closest('[data-fleet-toggle]');
    if (toggle && root.contains(toggle)) {
      const key = toggle.dataset.fleetToggle;
      if (collapsed.has(key)) {
        collapsed.delete(key);
      } else {
        collapsed.add(key);
      }
      saveCollapsed();
      applyCollapsed();
      return;
    }
    const expand = event.target.closest('[data-fleet-expand]');
    if (expand) {
      collapsed.clear();
      if (expand.dataset.fleetExpand === 'none') {
        root.querySelectorAll('[data-fleet-toggle]').forEach((button) => {
          if (button.dataset.fleetToggle !== 'fleet') {
            collapsed.add(button.dataset.fleetToggle);
          }
        });
      }
      saveCollapsed();
      applyCollapsed();
      return;
    }
    const row = event.target.closest('.fleet-function-row[role="button"]');
    if (row && !event.target.closest('a, button, form')) {
      activeFunction = activeFunction === row.dataset.functionFilter ? null : row.dataset.functionFilter;
      applyHighlight();
    }
  });

  root.addEventListener('keydown', (event) => {
    const row = event.target.closest('.fleet-function-row[role="button"]');
    if (row && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      row.click();
    }
  });

  root.addEventListener('change', (event) => {
    if (event.target.id === 'fleet-live-toggle') {
      storage.set(liveKey, event.target.checked ? '1' : '0');
      if (event.target.checked) {
        schedule(0);
      }
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      schedule(0);
    }
  });

  document.addEventListener('shown.bs.tab', (event) => {
    const target = event.target && event.target.dataset ? event.target.dataset.bsTarget : '';
    if (LIVE_TABS.includes(target)) {
      applyPending();
      schedule(0);
    }
  });

  document.addEventListener('hidden.bs.modal', () => window.setTimeout(applyPending, 0));
  document.addEventListener('hidden.bs.dropdown', () => window.setTimeout(applyPending, 0));
  document.addEventListener('focusout', () => window.setTimeout(applyPending, 0));

  const reloadHint = document.getElementById('fleet-reload-hint');
  if (reloadHint) {
    reloadHint.addEventListener('click', (event) => {
      // Reload with the address as it is, so the open tab stays open.
      event.preventDefault();
      window.location.reload();
    });
  }

  applyWriteState({ allowed: root.dataset.canWrite === '1', reason: '' });

  const readTime = document.getElementById('fleet-last-read');
  lastRead = readTime ? parseTime(readTime.dataset.time) : null;
  restore(new Set());
  tick();
  window.setInterval(tick, 1000);
  window.setInterval(applyPending, 1000);
  schedule();

  window.FleetOpsFleet = {
    root,
    applyState,
    request,
    getJSON,
    flash,
    isBusy,
    setBusy,
    onRender(callback) {
      renderCallbacks.push(callback);
      return () => {
        const index = renderCallbacks.indexOf(callback);
        if (index >= 0) {
          renderCallbacks.splice(index, 1);
        }
      };
    },
    selected,
    pollSoon,
    confirm: confirmAction,
    submit: submitAction,
  };
})();
