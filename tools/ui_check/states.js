// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// Scripted states of pages. run.js loads the page of a seeded page entry, calls the function named in its
// "states" list with {page, entry, lang, base} and checks the page it leaves behind like any other page.
// A state returns {expect: RegExp|null, note: string}: when "expect" is given and the page is in English, the
// text must be on the page afterwards, otherwise the state did not produce what it is for (a failure of its own).
//
// Selectors use field names, not ids or layout, so a redesigned page keeps working. A state that cannot find
// what it needs throws an Error that names it; run.js reports that as a failure of the page.
'use strict';

const ERROR_ELEMENTS = '.alert-danger, .alert-warning, .errorlist, [role=alert], .invalid-feedback, .fo-callout';

async function need(page, selector, what) {
  const handle = await page.$(selector);
  if (!handle) throw new Error(`state cannot find ${what} (${selector})`);
  return handle;
}

async function selectFirstOption(page, selector, what) {
  await need(page, selector, what);
  await page.evaluate((sel) => {
    const select = document.querySelector(sel);
    const option = [...select.options].find((o) => o.value);
    if (option) {
      select.value = option.value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }, selector);
}

// Send the form of an element the way a script-less browser would: no submit event, no script checks.
async function submitFormOf(page, selector) {
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'load', timeout: 20000 }),
    page.evaluate((sel) => {
      const form = document.querySelector(sel).closest('form');
      HTMLFormElement.prototype.submit.call(form);
    }, selector),
  ]);
}

const states = {
  // FB02: the start page with a ping Discord would refuse; the server answers with a page-level error.
  async 'start-error'({ page }) {
    await selectFirstOption(page, 'select[name=fc_character_id]', 'the FC character list');
    await selectFirstOption(page, 'select[name=fleet_type]', 'the fleet type list');
    await need(page, 'input[name=formup]', 'the form-up field');
    await page.fill('input[name=formup]', 'Jita');
    const channel = await page.evaluate(() => {
      const select = document.querySelector('select[name=ping_channel]');
      if (!select) return false;
      const option = [...select.options].find((o) => /Alliance Pings/.test(o.textContent));
      if (!option) return false;
      select.value = option.value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    });
    if (!channel) throw new Error('state cannot find the ping channel "Alliance Pings"');
    // "Send ping" is the full operation mode; the field exists in every layout of the page.
    await page.evaluate(() => {
      const mode = document.querySelector('input[name=operation_mode][value=full]');
      if (mode) mode.checked = true;
      const timing = document.querySelector('input[name=fleet_timing][value=ping_now]');
      if (timing) timing.checked = true;
    });
    await need(page, 'textarea[name=additional_message]', 'the additional information field');
    await page.fill('textarea[name=additional_message]', 'x'.repeat(2100));
    await submitFormOf(page, 'textarea[name=additional_message]');
    return { expect: /Discord accepts at most 2000/, note: 'the server answered with the ping-too-long error' };
  },

  // 5-13: the PAP tab of a running fleet, where the control for the PAP characters per player sits above the preview.
  async 'pap-tab'({ page }) {
    await need(page, '[data-bs-target="#attendance"]', 'the PAP tab');
    await page.click('[data-bs-target="#attendance"]');
    await page.waitForSelector('#attendance.show.active', { timeout: 5000 });
    // The pointer would otherwise stay on the tab and the check would measure its hover colour.
    await page.mouse.move(0, 0);
    return { expect: /PAP characters per player/, note: 'the PAP tab shows the control for the limit' };
  },

  // FB02: an SRP payout rule with an amount but neither ship nor doctrine; the error belongs to the whole form.
  async 'config-error'({ page }) {
    await need(page, 'input[name=amount]', 'the amount field');
    await page.fill('input[name=amount]', '5000000');
    await submitFormOf(page, 'input[name=amount]');
    return { expect: /Choose a ship type, a doctrine, or both/, note: 'the server answered with the form-level error' };
  },
};


// ---- Interaction states (harness v2): what a person does on a page, then the page is checked as it stands ----------

async function settleModal(page, selector) {
  await page.waitForSelector(`${selector}.show`, { timeout: 5000 });
  await page.waitForTimeout(250);
  await page.mouse.move(0, 0);
}

async function fillStartForm(page, { channelPattern = /Alliance Pings/, formup = 'Jita' } = {}) {
  await selectFirstOption(page, 'select[name=fc_character_id]', 'the FC character list');
  await selectFirstOption(page, 'select[name=fleet_type]', 'the fleet type list');
  await need(page, 'input[name=formup]', 'the form-up field');
  await page.fill('input[name=formup]', formup);
  await page.evaluate((source) => {
    const select = document.querySelector('select[name=ping_channel]');
    if (!select) return;
    const option = [...select.options].find((o) => new RegExp(source).test(o.textContent));
    if (option) {
      select.value = option.value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }, channelPattern.source);
}

// The preview is ready when the form is complete (nothing is listed as missing) and the raw ping text holds the
// built-in "FC Name" line (pings are English in every language, so this works for the Chinese pages as well).
async function waitForPreview(page, lookId) {
  await page.waitForFunction((id) => {
    const look = document.getElementById(id);
    const raw = document.getElementById(id.replace(/-look$/, '-preview'));
    const missing = document.getElementById('preview-missing');
    return look && raw && look.textContent.trim().length > 0 && /FC Name|Form Up|Group|mining/.test(raw.textContent)
      && !look.querySelector('.fo-skeleton') && (!missing || missing.classList.contains('d-none'));
  }, lookId, { timeout: 10000 });
}

Object.assign(states, {
  // The row actions (Move, Kick) appear while a member row is hovered.
  async 'hover-member'({ page }) {
    await need(page, '.fleet-member', 'a member row');
    await page.hover('.fleet-member');
    await page.waitForTimeout(250);
    return null;
  },

  // Keyboard: Tab into the fleet tree; the focus ring of the toggle is measured by the page checks.
  async 'focus-tree'({ page }) {
    await need(page, '.fleet-toggle', 'a tree toggle');
    await page.focus('.fleet-toggle');
    await page.keyboard.press('Tab');
    await page.keyboard.press('Shift+Tab');
    return null;
  },

  // The "..." menu of a member row.
  async 'member-menu'({ page }) {
    await need(page, '.fleet-member-menu [data-bs-toggle=dropdown]', 'a member menu button');
    await page.click('.fleet-member-menu [data-bs-toggle=dropdown]');
    await page.waitForSelector('.fleet-member-menu .dropdown-menu.show', { timeout: 5000 });
    return null;
  },

  // The rename field of a wing replaces the pen button.
  async rename({ page }) {
    await need(page, '[data-fleet-rename-open]', 'a rename button');
    await page.click('[data-fleet-rename-open]');
    await page.waitForSelector('form[data-fleet-rename]:not(.d-none) input[name=name]', { timeout: 5000 });
    return null;
  },

  // A function row of the composition highlights its pilots in the tree.
  async highlight({ page }) {
    await need(page, '.fleet-function-row[role=button]', 'a function row');
    await page.click('.fleet-function-row[role=button]');
    await page.waitForSelector('.fleet-member.fleet-highlight, .fleet-member.fleet-dim', { timeout: 5000 });
    return null;
  },

  // Tick a member, open the Move dialog from the bulk bar.
  async 'move-dialog'({ page }) {
    await need(page, 'input.fleet-select', 'a member checkbox');
    await page.check('input.fleet-select');
    await page.waitForSelector('#fleet-bulk-bar:not(.d-none)', { timeout: 5000 });
    await page.click('#fleet-bulk-move');
    await settleModal(page, '#fleet-move-modal');
    return null;
  },

  // The invite dialog loads its candidates from Alliance Auth.
  async invite({ page }) {
    await need(page, '#fleet-invite-open:not([disabled])', 'the invite button');
    await page.click('#fleet-invite-open');
    await settleModal(page, '#fleet-invite-modal');
    await page.waitForTimeout(800);
    return null;
  },

  // Drag a member over a squad: the drop marks are set and the drag image is a label (FB03).
  async drag({ page }) {
    await need(page, 'li.fleet-member[draggable=true]', 'a draggable member');
    await need(page, 'ul[data-drop-role=squad_member]', 'a squad drop target');
    const outcome = await page.evaluate(() => {
      const calls = [];
      const original = DataTransfer.prototype.setDragImage;
      DataTransfer.prototype.setDragImage = function (...args) { calls.push(args.length); return original && original.apply(this, args); };
      const member = document.querySelector('li.fleet-member[draggable=true]');
      const target = document.querySelector('ul[data-drop-role=squad_member]');
      const transfer = new DataTransfer();
      member.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: transfer }));
      target.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: transfer }));
      DataTransfer.prototype.setDragImage = original;
      return { calls: calls.length };
    });
    if (outcome.calls < 1) throw new Error('state: dragging a member did not set a drag image (setDragImage was not called)');
    await page.waitForTimeout(100);
    await page.waitForSelector('.fleet-drop-over', { timeout: 3000 });
    return null;
  },

  // The End Fleet confirmation.
  async 'confirm-end'({ page }) {
    if (await page.$('[data-bs-target="#endFleetModal"]')) {
      await page.click('[data-bs-target="#endFleetModal"]');
      await settleModal(page, '#endFleetModal');
    } else {
      await need(page, 'form.end-fleet-form button', 'the End Fleet button');
      await page.click('form.end-fleet-form button');
      await settleModal(page, '#fo-confirm');
    }
    return null;
  },

  // The start page with a channel: the preview is rendered; then the raw text view.
  async 'start-preview'({ page }) {
    await fillStartForm(page);
    await waitForPreview(page, 'ping-look');
    const raw = await page.$('#ping-view-raw');
    if (raw) {
      await page.evaluate(() => document.getElementById('ping-view-raw').click());
      await page.waitForSelector('#ping-preview:not([hidden])', { timeout: 3000 });
    }
    return null;
  },

  // Submitting with a channel asks first: the dialog is open and nothing is sent.
  async 'start-confirm'({ page }) {
    await fillStartForm(page);
    await waitForPreview(page, 'ping-look');
    await page.evaluate(() => {
      const mode = document.querySelector('input[name=operation_mode][value=full]');
      if (mode) mode.checked = true;
      const timing = document.querySelector('input[name=fleet_timing][value=ping_now]');
      if (timing) timing.checked = true;
    });
    await page.click('#fleet-start-submit');
    await page.waitForSelector('#fo-confirm.show, #fo-confirm[open], #fo-confirm.is-open', { timeout: 5000 });
    await page.mouse.move(0, 0);
    return null;
  },

  // A group leader's start page: the group's own types and channels, the group line in the preview.
  async 'start-group'({ page }) {
    await need(page, 'input[name=fleet_group]', 'the group field');
    await selectFirstOption(page, 'select[name=fc_character_id]', 'the FC character list');
    await selectFirstOption(page, 'select[name=fleet_type]', 'the fleet type list');
    await page.fill('input[name=formup]', 'Jita');
    await selectFirstOption(page, 'select[name=ping_channel]', 'the channel list');
    await waitForPreview(page, 'ping-look');
    const text = await page.evaluate(() => document.getElementById('ping-preview').textContent);
    if (!/Group/.test(text)) throw new Error('state: the preview of a group fleet has no group line');
    return null;
  },

  // Send Ping: the preview of the ping, then the question before it is posted.
  async 'send-ping-preview'({ page }) {
    await need(page, 'select[name=ping_channel]', 'the channel list');
    await selectFirstOption(page, 'select[name=ping_channel]', 'the channel list');
    await page.fill('textarea[name=additional_message]', 'Form up in Jita for a mining op');
    await waitForPreview(page, 'ping-look');
    return null;
  },

  async 'send-ping-confirm'({ page }) {
    await selectFirstOption(page, 'select[name=ping_channel]', 'the channel list');
    await page.fill('textarea[name=additional_message]', 'Form up in Jita for a mining op');
    await waitForPreview(page, 'ping-look');
    await page.click('#send-ping-submit');
    await page.waitForSelector('#fo-confirm.show, #fo-confirm[open], #fo-confirm.is-open', { timeout: 5000 });
    await page.mouse.move(0, 0);
    return null;
  },

  // Reject on the approvals page asks first.
  async 'confirm-reject'({ page }) {
    await need(page, 'button[name=action][value=reject][data-fo-confirm]', 'a Reject button');
    await page.click('button[name=action][value=reject][data-fo-confirm]');
    await page.waitForSelector('#fo-confirm.show, #fo-confirm[open], #fo-confirm.is-open', { timeout: 5000 });
    await page.mouse.move(0, 0);
    return null;
  },

  // Tick a claim: the bulk bar is enabled and the count follows.
  async 'srp-select'({ page }) {
    await need(page, 'input[data-srp-select]', 'a claim checkbox');
    await page.check('input[data-srp-select]');
    await page.waitForSelector('[data-srp-bulk-button]:not([disabled])', { timeout: 3000 });
    return null;
  },

  // The month and year selectors of the statistics pages: the year list is focused and opened by keyboard.
  async 'month-pick'({ page }) {
    await need(page, '#fleetops-period-month', 'the month selector');
    await page.focus('#fleetops-period-month');
    await page.keyboard.press('ArrowUp');
    return null;
  },

  // Every notification level of FleetOps.toast.
  async toast({ page }) {
    const shown = await page.evaluate(() => {
      if (!window.FleetOps || typeof window.FleetOps.toast !== 'function') return -1;
      for (const level of ['success', 'info', 'warning', 'danger']) window.FleetOps.toast(`A ${level} notice`, level);
      return document.querySelectorAll('.fo-toast').length;
    });
    if (shown < 1) throw new Error('state: FleetOps.toast showed nothing');
    await page.waitForTimeout(300);
    return null;
  },

  // The loss check of SRP shows the placeholder card while the answer is awaited (the request itself is stopped
  // before it leaves the page, so the page stays in the waiting state).
  async 'srp-check-loading'({ page }) {
    await page.evaluate(() => window.addEventListener('submit', (event) => event.preventDefault(), true));
    await need(page, 'form[data-srp-check]', 'the loss check form');
    await page.evaluate(() => {
      const form = document.querySelector('form[data-srp-check]');
      form.querySelectorAll('textarea, input[type=text], input[type=url], input:not([type])').forEach((input) => {
        if (!input.value) input.value = 'https://zkillboard.com/kill/123456789/';
      });
      form.querySelectorAll('select').forEach((select) => {
        const option = [...select.options].find((o) => o.value);
        if (option && !select.value) select.value = option.value;
      });
    });
    await page.click('[data-srp-check-button]');
    await page.waitForSelector('[data-srp-result] .fo-skeleton', { timeout: 5000 });
    return null;
  },
});

module.exports = { states, ERROR_ELEMENTS };
