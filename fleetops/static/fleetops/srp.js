// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// FleetOps SRP page helpers: copy buttons, selection totals, the approve form, the loading state of the loss check
// and a guard against double clicks.
// Every form works without this script; it only makes the pages quicker to use.
(() => {
  'use strict';

  const COPIED_MS = 1600;

  // ISK as the pages write it: 1,234,567 ISK.
  function formatIsk(amount) {
    return `${Math.round(amount).toLocaleString('en-US')} ISK`;
  }

  // The amounts people type: 250m, 1.2b, 750k, 250,000,000, 1 000 000. The server checks the final value;
  // this only drives the hint, so null (not understood) just means no hint.
  function parseIsk(text) {
    const cleaned = String(text).replace(/[  \s]+/g, ' ').trim().toLowerCase().replace(/\s*isk$/, '');
    const match = /^(.+?)\s*([kmb]?)$/.exec(cleaned);
    if (!match) return null;
    const number = match[1];
    const plain = /^\d+(\.\d+)?$/.test(number);
    const commas = /^\d{1,3}(,\d{3})+(\.\d+)?$/.test(number);
    const spaces = /^\d{1,3}( \d{3})+(\.\d+)?$/.test(number);
    if (!plain && !commas && !spaces) return null;
    const factor = { '': 1, k: 1e3, m: 1e6, b: 1e9 }[match[2]];
    const value = Number(number.replace(/[, ]/g, '')) * factor;
    return Number.isFinite(value) ? Math.round(value) : null;
  }

  // --- Copy buttons -----------------------------------------------------------------------------------

  // The shared clipboard helper of the page (fleetops.js) with its fallback; a page without it still copies.
  function copyText(text) {
    if (window.FleetOps && typeof window.FleetOps.copyText === 'function') {
      return window.FleetOps.copyText(text);
    }
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(() => true, () => false);
    }
    return Promise.resolve(false);
  }

  function showCopied(button, ok) {
    if (button.dataset.srpLabel === undefined) button.dataset.srpLabel = button.textContent;
    button.textContent = ok ? gettext('Copied') : gettext('Copy failed');
    window.clearTimeout(Number(button.dataset.srpTimer || 0));
    button.dataset.srpTimer = String(window.setTimeout(() => {
      button.textContent = button.dataset.srpLabel;
    }, COPIED_MS));
  }

  document.querySelectorAll('[data-srp-copy]').forEach((button) => {
    // The buttons are hidden until this script runs, because they do nothing without it.
    button.hidden = false;
  });
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-srp-copy]');
    if (!button) return;
    event.preventDefault();
    copyText(button.dataset.srpCopy).then((ok) => showCopied(button, ok));
  });

  // "Request SRP" in the empty list of My SRP takes the pilot to the claim form and puts the cursor in it.
  document.addEventListener('click', (event) => {
    const link = event.target.closest('a[href="#srp-claim-form"]');
    if (!link) return;
    const form = document.getElementById('srp-claim-form');
    const field = form && form.querySelector('textarea, input:not([type="hidden"])');
    if (!field) return;
    event.preventDefault();
    field.focus();
  });

  // --- Selecting claims -------------------------------------------------------------------------------

  function updateSelection(form) {
    const boxes = Array.from(form.querySelectorAll('[data-srp-select]'));
    const ticked = boxes.filter((box) => box.checked);
    const total = ticked.reduce((sum, box) => sum + (Number(box.dataset.srpAmount) || 0), 0);
    form.querySelectorAll('[data-srp-selected-count]').forEach((node) => { node.textContent = String(ticked.length); });
    form.querySelectorAll('[data-srp-selected-total]').forEach((node) => { node.textContent = formatIsk(total); });
    form.querySelectorAll('[data-srp-bulk-button]').forEach((button) => { button.disabled = ticked.length === 0 || form.dataset.srpBusy === '1'; });
    form.querySelectorAll('[data-srp-select-group]').forEach((box) => {
      const own = Array.from(box.closest('[data-srp-group]').querySelectorAll('[data-srp-select]'));
      const count = own.filter((item) => item.checked).length;
      box.checked = own.length > 0 && count === own.length;
      box.indeterminate = count > 0 && count < own.length;
    });
    form.querySelectorAll('[data-srp-select-all]').forEach((box) => {
      box.checked = boxes.length > 0 && ticked.length === boxes.length;
      box.indeterminate = ticked.length > 0 && ticked.length < boxes.length;
    });
  }

  document.addEventListener('change', (event) => {
    const box = event.target;
    if (!(box instanceof HTMLInputElement)) return;
    const form = box.form || box.closest('form');
    if (!form) return;
    if (box.matches('[data-srp-select-all]')) {
      form.querySelectorAll('[data-srp-select]').forEach((item) => { item.checked = box.checked; });
    } else if (box.matches('[data-srp-select-group]')) {
      box.closest('[data-srp-group]').querySelectorAll('[data-srp-select]').forEach((item) => { item.checked = box.checked; });
    } else if (!box.matches('[data-srp-select]')) {
      return;
    }
    updateSelection(form);
  });
  document.querySelectorAll('form').forEach((form) => {
    if (form.querySelector('[data-srp-select]')) updateSelection(form);
  });

  // --- The approve form ---------------------------------------------------------------------------------

  document.querySelectorAll('.fleetops-srp-approve').forEach((form) => {
    const amount = form.querySelector('[name="amount"]');
    const reason = form.querySelector('[name="reason"]');
    const hint = form.querySelector('[data-srp-reason-hint]');
    const suggested = Number(form.dataset.srpSuggested) || 0;
    const limited = Number(form.dataset.srpLimited) || 0;
    // Set on an approved claim: any other amount is a change, and a change always needs its reason.
    const approved = form.dataset.srpApproved === undefined ? null : Number(form.dataset.srpApproved);
    const plainHint = hint ? hint.textContent : '';

    function differs() {
      const typed = parseIsk(amount.value);
      if (typed === null) return false;
      return typed !== (approved === null ? suggested : approved);
    }

    function refreshHint() {
      if (!hint) return;
      const needed = differs() && !reason.value.trim();
      hint.textContent = needed
        ? (approved === null
          ? gettext('Reason required: the amount differs from the suggested amount.')
          : gettext('Reason required: say why the amount changes.'))
        : plainHint;
      hint.classList.toggle('text-danger', needed);
      reason.classList.toggle('is-invalid', needed);
    }

    amount.addEventListener('input', refreshHint);
    reason.addEventListener('input', refreshHint);

    form.addEventListener('click', (event) => {
      const button = event.target.closest('button[name="mode"]');
      if (!button) return;
      if (button.value === 'suggested' && suggested) {
        amount.value = suggested.toLocaleString('en-US');
      } else if (button.value === 'within_limit') {
        amount.value = limited.toLocaleString('en-US');
        if (!reason.value.trim() && form.dataset.srpLimitReason) reason.value = form.dataset.srpLimitReason;
      } else if (button.value === 'amount' && differs() && !reason.value.trim()) {
        event.preventDefault();
        refreshHint();
        reason.focus();
      }
    });
  });

  // --- A guard against double clicks and the loading state of the loss check ----------------------------------

  // The check of a loss asks zKillboard and ESI and can take several seconds: the button says so and a grey
  // placeholder card holds the place of the answer until the page comes back.
  function skeletonCard() {
    const card = document.createElement('div');
    card.className = 'fo-loss-card';
    card.setAttribute('aria-hidden', 'true');
    const render = document.createElement('span');
    render.className = 'fo-skeleton fo-srp-skeleton-render';
    const lines = document.createElement('div');
    for (const width of ['fo-srp-skeleton-wide', 'fo-srp-skeleton-mid', 'fo-srp-skeleton-short']) {
      const line = document.createElement('span');
      line.className = `fo-skeleton ${width}`;
      lines.appendChild(line);
    }
    card.append(render, lines);
    return card;
  }

  function showChecking(form) {
    // The words come from the page (data-srp-checking), written in the language of the viewer.
    const button = form.querySelector('[data-srp-check-button]');
    const words = button ? button.dataset.srpChecking || '' : '';
    if (button) {
      if (button.dataset.srpLabel === undefined) button.dataset.srpLabel = button.textContent;
      if (words) button.textContent = words;
    }
    const result = form.querySelector('[data-srp-result]');
    if (result) {
      const status = document.createElement('span');
      status.className = 'visually-hidden';
      status.textContent = words;
      result.replaceChildren(status, skeletonCard());
      result.hidden = false;
    }
  }

  function hideChecking(form) {
    const button = form.querySelector('[data-srp-check-button]');
    if (button && button.dataset.srpLabel !== undefined) button.textContent = button.dataset.srpLabel;
    const result = form.querySelector('[data-srp-result]');
    if (result) {
      result.replaceChildren();
      result.hidden = true;
    }
  }

  function setBusy(form, busy) {
    form.dataset.srpBusy = busy ? '1' : '0';
    form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach((button) => { button.disabled = busy; });
    if (!busy && form.querySelector('[data-srp-select]')) updateSelection(form);
    if (!busy && form.hasAttribute('data-srp-check')) hideChecking(form);
  }

  // A question the form asks before it is sent (data-fo-confirm) is handled by fleetops.js, which stops the
  // first submit; this handler only sees the confirmed one.
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-srp-once')) return;
    if (form.dataset.srpBusy === '1') {
      event.preventDefault();
      return;
    }
    // Disabled buttons are left out of the form data, so the buttons are disabled after the browser has read it.
    form.dataset.srpBusy = '1';
    window.setTimeout(() => setBusy(form, true), 0);
    if (form.hasAttribute('data-srp-check')) showChecking(form);
  });

  // Coming back to a page from the history leaves the buttons as they were: make them usable again.
  window.addEventListener('pageshow', (event) => {
    if (!event.persisted) return;
    document.querySelectorAll('form[data-srp-once]').forEach((form) => setBusy(form, false));
  });

  window.FleetOpsSrp = { parseIsk, formatIsk };
})();
